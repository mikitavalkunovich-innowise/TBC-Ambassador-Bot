"""
Telegram channel subscription check handler.
Verifies membership before allowing image generation.
"""
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.builders import subscribed_keyboard
from app.bot.states import UserFlow
from app.models.event import EventType
from app.models.user import FlowStatus, User
from app.services import settings_service
from app.services.analytics_service import track_event

router = Router(name="subscription")
logger = logging.getLogger(__name__)


async def _is_subscribed(bot: Bot, channel_id: str, user_id: int) -> bool:
    """
    Return True only if the user is a confirmed member of the channel.

    Statuses that count as subscribed: member, administrator, creator, restricted
    (restricted users are still in the channel).
    Statuses that mean NOT subscribed: left, kicked, banned.

    On error we return False (fail-closed) so that a misconfigured bot token
    or missing admin rights does not silently bypass the gate.
    """
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        return member.status not in ("left", "kicked", "banned")
    except TelegramBadRequest as e:
        logger.error(
            "Subscription check FAILED for user %d in channel %s: %s — "
            "make sure the bot is an admin of the channel.",
            user_id, channel_id, e,
        )
        return False
    except Exception:
        logger.exception(
            "Unexpected error checking channel membership for user %d", user_id
        )
        return False


async def send_subscription_prompt(
    message: Message,
    user: User,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """Send the channel subscription prompt to the user."""
    lang = user.language.value if user.language else "ru"
    channel_id = await settings_service.get(session, "telegram_channel_id") or ""

    # Build channel link for the button
    if channel_id.startswith("-100"):
        # Private channel — can't build a direct t.me link
        channel_link = "https://t.me"
    elif channel_id.startswith("@"):
        channel_link = f"https://t.me/{channel_id.lstrip('@')}"
    else:
        channel_link = f"https://t.me/{channel_id.lstrip('@')}"

    sub_text_raw = await settings_service.get_text(session, "msg_subscribe", lang)
    sub_text = sub_text_raw.format(channel_link=channel_link)

    await message.answer(
        sub_text,
        reply_markup=subscribed_keyboard(lang, channel_link),
    )
    await state.set_state(UserFlow.checking_subscription)


@router.callback_query(F.data == "sub:check")
async def handle_subscription_check(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
) -> None:
    """User pressed 'I subscribed' — verify and proceed."""
    tg_id = callback.from_user.id
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()

    if user is None:
        await callback.answer("Please send /start first.")
        return

    lang = user.language.value if user.language else "ru"

    # Guard: user must have accepted privacy but not yet passed the subscription gate.
    # This covers FSM state loss after app restart — the DB flow_status is the source of truth.
    # If the user is already past this step (VIDEO_SEEN or further), ignore silently.
    if user.flow_status != FlowStatus.PRIVACY_ACCEPTED:
        await callback.answer()
        return
    channel_id = await settings_service.get(session, "telegram_channel_id") or ""

    if channel_id and not await _is_subscribed(bot, channel_id, tg_id):
        not_sub_text = await settings_service.get_text(session, "msg_not_subscribed", lang)
        await callback.answer(not_sub_text, show_alert=True)
        return

    # User is subscribed — record and proceed to photo
    from datetime import datetime, timezone
    user.channel_subscribed_at = datetime.now(timezone.utc)
    user.flow_status = FlowStatus.VIDEO_SEEN  # all gates passed, ready for photo upload
    await session.flush()

    await track_event(session, user.id, EventType.CHANNEL_SUBSCRIBED)

    await callback.message.edit_reply_markup(reply_markup=None)

    from app.bot.handlers.photo import send_photo_request
    await send_photo_request(callback.message, user, session, state)
    await callback.answer()
