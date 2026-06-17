"""Language selection handler."""
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.privacy import send_disclaimer
from app.bot.states import UserFlow
from app.models.event import EventType
from app.models.user import FlowStatus, Language, User
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

    await callback.message.edit_reply_markup(reply_markup=None)
    await send_disclaimer(callback.message, user, session, state)
    await callback.answer()
