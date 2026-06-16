"""Language selection handler."""
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.builders import agree_keyboard
from app.bot.states import UserFlow
from app.models.event import EventType
from app.models.user import FlowStatus, Language, User
from app.services import settings_service
from app.services.analytics_service import track_event

router = Router(name="language")
logger = logging.getLogger(__name__)


@router.callback_query(UserFlow.selecting_language, F.data.startswith("lang:"))
async def handle_language_selection(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    lang_code = callback.data.split(":")[1]  # "ru" or "uz"
    lang = Language.RU if lang_code == "ru" else Language.UZ

    tg_id = callback.from_user.id
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()

    if user is None:
        await callback.answer("Please send /start first.")
        return

    user.language = lang
    user.flow_status = FlowStatus.LANGUAGE_SET
    await session.flush()

    await track_event(session, user.id, EventType.LANGUAGE_SET)

    # Show privacy policy
    privacy_url = await settings_service.get(session, "privacy_policy_url") or ""
    privacy_text_raw = await settings_service.get_text(session, "msg_privacy", lang_code)
    privacy_text = privacy_text_raw.format(privacy_url=privacy_url)

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        privacy_text,
        reply_markup=agree_keyboard(lang_code),
        disable_web_page_preview=False,
    )
    await state.set_state(UserFlow.awaiting_privacy)
    await callback.answer()
