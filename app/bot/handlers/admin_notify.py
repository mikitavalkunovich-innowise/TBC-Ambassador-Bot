"""
Admin Telegram moderation callbacks.
Handles approve/reject inline button presses from the admin's Telegram chat.
"""
import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.builders import share_bot_keyboard
from app.core.config import get_settings
from app.models.event import EventType
from app.models.generation import GeneratedImage, ImageStatus
from app.models.user import FlowStatus, User
from app.services import settings_service
from app.services.analytics_service import track_event
from app.services.notify_service import (
    edit_approval_message_approved,
    edit_approval_message_rejected,
    APPROVE_CB,
    REJECT_CB,
)

router = Router(name="admin_notify")
logger = logging.getLogger(__name__)


async def _send_no_attempts_message(bot, user: User, session: AsyncSession, lang: str) -> None:
    """Send the 'no attempts left' message with optional share-bot button."""
    no_attempts_text = await settings_service.get_text(session, "msg_no_attempts_left", lang)
    bot_username = await settings_service.get(session, "bot_username") or ""
    markup = share_bot_keyboard(lang, bot_username) if bot_username else None
    await bot.send_message(chat_id=user.telegram_id, text=no_attempts_text, reply_markup=markup)


async def _send_result_to_user(
    bot,
    user: User,
    image: GeneratedImage,
    approved: bool,
    session: AsyncSession,
) -> None:
    """Send the approved image or rejection message to the user."""
    lang = user.language.value if user.language else "ru"

    if approved:
        approved_text = await settings_service.get_text(session, "msg_approved", lang)
        sent = False
        # Priority 1: resend via Telegram file_id (no disk access needed)
        if image.telegram_image_file_id:
            try:
                await bot.send_photo(
                    chat_id=user.telegram_id,
                    photo=image.telegram_image_file_id,
                    caption=approved_text,
                )
                sent = True
            except Exception:
                logger.warning("Failed to send via file_id, falling back to local file")
        # Priority 2: local file on disk
        if not sent and image.image_path:
            from app.core.storage import get_absolute_path
            from aiogram.types import FSInputFile
            img_path = get_absolute_path(image.image_path)
            if img_path.exists():
                try:
                    await bot.send_photo(
                        chat_id=user.telegram_id,
                        photo=FSInputFile(str(img_path)),
                        caption=approved_text,
                    )
                    sent = True
                except Exception:
                    logger.warning("Failed to send via local file for image %s", image.id)
        if not sent:
            await bot.send_message(chat_id=user.telegram_id, text=approved_text)
    else:
        rejection_text = await settings_service.get_text(session, "rejection_message", lang)
        await bot.send_message(chat_id=user.telegram_id, text=rejection_text)


@router.callback_query(F.data.startswith(f"{APPROVE_CB}:"))
async def handle_approve(
    callback: CallbackQuery,
    session: AsyncSession,
) -> None:
    from app.bot.instance import get_bot

    image_id = callback.data.split(":", 1)[1]
    bot = get_bot()
    config = get_settings()

    result = await session.execute(select(GeneratedImage).where(GeneratedImage.id == image_id))
    image = result.scalar_one_or_none()

    if image is None:
        await callback.answer("Image not found.", show_alert=True)
        return

    if image.status != ImageStatus.PENDING:
        await callback.answer("Already reviewed.", show_alert=True)
        return

    # Acknowledge the callback immediately — Telegram requires this within 30 seconds.
    # All subsequent work (send photo, video, regen prompt, edit message) can take longer.
    await callback.answer("Approved ✅")

    # Update image status
    image.status = ImageStatus.APPROVED
    image.reviewed_at = datetime.now(timezone.utc)
    await session.flush()

    # Load user
    user = await session.get(User, image.user_id)
    if user is None:
        logger.error("User not found for image_id=%s (user_id=%s)", image_id, image.user_id)
        return

    # Check remaining attempts
    max_attempts_str = await settings_service.get(session, "max_regeneration_attempts") or "3"
    max_attempts = int(max_attempts_str)
    attempts_remaining = max_attempts - user.regenerations_used

    user.flow_status = FlowStatus.DONE
    await session.flush()

    await track_event(session, user.id, EventType.IMAGE_APPROVED)

    # Send result to user
    await _send_result_to_user(bot, user, image, approved=True, session=session)

    lang = user.language.value if user.language else "ru"

    try:
        from app.bot.handlers.media import send_card_promo_after_result
        await send_card_promo_after_result(bot, user.telegram_id, user, session)
    except Exception:
        logger.exception("Failed to send card promo to user %d", user.telegram_id)

    # Optionally send the bonus Eldor video after the approved image
    video_enabled = await settings_service.get(session, "video_enabled") == "1"
    if video_enabled:
        try:
            from app.bot.handlers.media import send_video_after_result
            await send_video_after_result(bot, user.telegram_id, user, session)
        except Exception:
            logger.exception("Failed to send bonus video to user %d", user.telegram_id)

    # Offer regeneration if attempts remain
    if attempts_remaining > 0:
        from app.bot.keyboards.builders import regenerate_keyboard
        from app.bot.instance import set_user_fsm_state
        from app.bot.states import UserFlow

        regen_key = "msg_regenerate_1left" if attempts_remaining == 1 else "msg_regenerate_2left"
        text = await settings_service.get_text(session, regen_key, lang)
        await bot.send_message(
            chat_id=user.telegram_id,
            text=text,
            reply_markup=regenerate_keyboard(lang),
        )
        # Advance FSM state so the callback keyboard works
        await set_user_fsm_state(user.telegram_id, UserFlow.awaiting_regeneration_input)
    else:
        await _send_no_attempts_message(bot, user, session, lang)
        await track_event(session, user.id, EventType.FLOW_COMPLETED)

    # Edit the admin Telegram notification
    if image.admin_tg_chat_id and image.admin_tg_message_id:
        await edit_approval_message_approved(
            bot=bot,
            chat_id=image.admin_tg_chat_id,
            message_id=image.admin_tg_message_id,
            telegram_username=user.telegram_username,
            telegram_id=user.telegram_id,
            admin_base_url=config.webhook_base_url,
        )


@router.callback_query(F.data.startswith(f"{REJECT_CB}:"))
async def handle_reject(
    callback: CallbackQuery,
    session: AsyncSession,
) -> None:
    from app.bot.instance import get_bot

    image_id = callback.data.split(":", 1)[1]
    bot = get_bot()
    config = get_settings()

    result = await session.execute(select(GeneratedImage).where(GeneratedImage.id == image_id))
    image = result.scalar_one_or_none()

    if image is None:
        await callback.answer("Image not found.", show_alert=True)
        return

    if image.status != ImageStatus.PENDING:
        await callback.answer("Already reviewed.", show_alert=True)
        return

    # Acknowledge immediately — same reason as handle_approve.
    await callback.answer("Rejected ❌")

    image.status = ImageStatus.REJECTED
    image.reviewed_at = datetime.now(timezone.utc)
    await session.flush()

    user = await session.get(User, image.user_id)
    if user is None:
        logger.error("User not found for image_id=%s (user_id=%s)", image_id, image.user_id)
        return

    await track_event(session, user.id, EventType.IMAGE_REJECTED)

    # Send rejection to user
    await _send_result_to_user(bot, user, image, approved=False, session=session)

    # Offer regeneration if attempts remain
    max_attempts_str = await settings_service.get(session, "max_regeneration_attempts") or "3"
    max_attempts = int(max_attempts_str)
    attempts_remaining = max_attempts - user.regenerations_used

    lang = user.language.value if user.language else "ru"
    user.flow_status = FlowStatus.DONE
    await session.flush()

    if attempts_remaining > 0:
        from app.bot.keyboards.builders import regenerate_keyboard
        from app.bot.instance import set_user_fsm_state
        from app.bot.states import UserFlow

        regen_key = "msg_regenerate_1left" if attempts_remaining == 1 else "msg_regenerate_2left"
        text = await settings_service.get_text(session, regen_key, lang)
        await bot.send_message(
            chat_id=user.telegram_id,
            text=text,
            reply_markup=regenerate_keyboard(lang),
        )
        await set_user_fsm_state(user.telegram_id, UserFlow.awaiting_regeneration_input)
    else:
        await _send_no_attempts_message(bot, user, session, lang)
        await track_event(session, user.id, EventType.FLOW_COMPLETED)

    # Edit admin message
    if image.admin_tg_chat_id and image.admin_tg_message_id:
        await edit_approval_message_rejected(
            bot=bot,
            chat_id=image.admin_tg_chat_id,
            message_id=image.admin_tg_message_id,
            telegram_username=user.telegram_username,
            telegram_id=user.telegram_id,
            admin_base_url=config.webhook_base_url,
        )
