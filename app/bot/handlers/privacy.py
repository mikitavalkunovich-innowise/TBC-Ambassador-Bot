"""Disclaimer / privacy agreement handler."""
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.builders import disclaimer_keyboard
from app.bot.states import UserFlow
from app.models.event import EventType
from app.models.user import FlowStatus, User
from app.services import settings_service
from app.services.analytics_service import track_event

router = Router(name="privacy")
logger = logging.getLogger(__name__)


async def send_disclaimer(
    message: Message,
    user: User,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """Show the legal disclaimer screen after language selection."""
    lang = user.language.value if user.language else "ru"

    disclaimer_text = await settings_service.get_text(session, "msg_privacy", lang)

    link_enabled = await settings_service.get(session, "privacy_policy_link_enabled") == "1"
    privacy_url = (await settings_service.get(session, "privacy_policy_url") or "").strip()
    show_link = link_enabled and bool(privacy_url)

    await message.answer(
        disclaimer_text,
        reply_markup=disclaimer_keyboard(lang, privacy_url if show_link else None),
        disable_web_page_preview=True,
    )
    await state.set_state(UserFlow.awaiting_privacy)


@router.callback_query(UserFlow.awaiting_privacy, F.data == "privacy:agree")
async def handle_privacy_agreed(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    tg_id = callback.from_user.id
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()

    if user is None:
        await callback.answer("Please send /start first.")
        return

    user.privacy_accepted = True
    user.flow_status = FlowStatus.PRIVACY_ACCEPTED
    await session.flush()

    await track_event(session, user.id, EventType.PRIVACY_ACCEPTED)

    await callback.message.edit_reply_markup(reply_markup=None)

    # Proceed to video step
    from app.bot.handlers.media import send_video_message
    await send_video_message(callback.message, user, session)
    await state.set_state(UserFlow.awaiting_video_action)
    await callback.answer()
