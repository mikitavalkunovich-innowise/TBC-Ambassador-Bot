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
        if image.image_path:
            from app.core.storage import get_absolute_path
            from aiogram.types import FSInputFile
            img_path = get_absolute_path(image.image_path)
            if img_path.exists():
                await bot.send_photo(
                    chat_id=user.telegram_id,
                    photo=FSInputFile(str(img_path)),
                    caption=approved_text,
                )
            else:
                await bot.send_message(chat_id=user.telegram_id, text=approved_text)
        else:
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

    # Update image status
    image.status = ImageStatus.APPROVED
    image.reviewed_at = datetime.now(timezone.utc)
    await session.flush()

    # Load user
    user = await session.get(User, image.user_id)
    if user is None:
        await callback.answer("User not found.", show_alert=True)
        return

    # Check remaining attempts
    max_attempts_str = await settings_service.get(session, "max_regeneration_attempts") or "3"
    max_attempts = int(max_attempts_str)
    attempts_remaining = max_attempts - user.regenerations_used

    # Update user flow status
    user.flow_status = FlowStatus.DONE if attempts_remaining <= 0 else FlowStatus.DONE
    await session.flush()

    await track_event(session, user.id, EventType.IMAGE_APPROVED)

    # Send result to user
    await _send_result_to_user(bot, user, image, approved=True, session=session)

    # Offer regeneration if attempts remain
    if attempts_remaining > 0:
        from app.bot.handlers.photo import send_regenerate_prompt
        from aiogram.types import Message
        # We need to send to the user's chat directly
        lang = user.language.value if user.language else "ru"
        text = await settings_service.get_text(session, "msg_regenerate_prompt", lang)
        from app.bot.keyboards.builders import regenerate_keyboard
        await bot.send_message(
            chat_id=user.telegram_id,
            text=text,
            reply_markup=regenerate_keyboard(lang),
        )
    else:
        lang = user.language.value if user.language else "ru"
        no_attempts_text = await settings_service.get_text(session, "msg_no_attempts_left", lang)
        await bot.send_message(chat_id=user.telegram_id, text=no_attempts_text)
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

    await callback.answer("Approved ✅")


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

    image.status = ImageStatus.REJECTED
    image.reviewed_at = datetime.now(timezone.utc)
    await session.flush()

    user = await session.get(User, image.user_id)
    if user is None:
        await callback.answer("User not found.", show_alert=True)
        return

    await track_event(session, user.id, EventType.IMAGE_REJECTED)

    # Send rejection to user
    await _send_result_to_user(bot, user, image, approved=False, session=session)

    # Offer regeneration if attempts remain
    max_attempts_str = await settings_service.get(session, "max_regeneration_attempts") or "3"
    max_attempts = int(max_attempts_str)
    attempts_remaining = max_attempts - user.regenerations_used

    if attempts_remaining > 0:
        lang = user.language.value if user.language else "ru"
        text = await settings_service.get_text(session, "msg_regenerate_prompt", lang)
        from app.bot.keyboards.builders import regenerate_keyboard
        user.flow_status = FlowStatus.DONE
        await session.flush()
        await bot.send_message(
            chat_id=user.telegram_id,
            text=text,
            reply_markup=regenerate_keyboard(lang),
        )
    else:
        lang = user.language.value if user.language else "ru"
        user.flow_status = FlowStatus.DONE
        await session.flush()
        no_attempts_text = await settings_service.get_text(session, "msg_no_attempts_left", lang)
        await bot.send_message(chat_id=user.telegram_id, text=no_attempts_text)
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

    await callback.answer("Rejected ❌")
