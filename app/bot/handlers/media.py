"""
Video delivery handler.
Sends the configured video (URL or file) with accompanying text,
then shows the "Generate Image" button.
"""
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.builders import generate_keyboard
from app.bot.states import UserFlow
from app.core.storage import get_absolute_path
from app.models.event import EventType
from app.models.user import FlowStatus, User
from app.services import settings_service
from app.services.analytics_service import track_event

router = Router(name="media")
logger = logging.getLogger(__name__)


async def send_video_message(message: Message, user: User, session: AsyncSession) -> None:
    """Send the configured video + text to the user."""
    lang = user.language.value if user.language else "ru"

    video_url = await settings_service.get(session, f"video_url_{lang}")
    video_path_rel = await settings_service.get(session, f"video_path_{lang}")
    video_text = await settings_service.get_text(session, "msg_video", lang)

    if video_url:
        await message.answer_video(video=video_url, caption=video_text)
    elif video_path_rel:
        video_path = get_absolute_path(video_path_rel)
        if video_path.exists():
            from aiogram.types import FSInputFile
            await message.answer_video(video=FSInputFile(str(video_path)), caption=video_text)
        else:
            logger.warning("Video file not found: %s", video_path)
            await message.answer(video_text)
    else:
        # No video configured — just send the text
        await message.answer(video_text)

    await send_generate_prompt(message, lang, session)


async def send_generate_prompt(message: Message, lang: str, session: AsyncSession) -> None:
    """Show the 'Generate Image' button to the user."""
    result = await session.execute(
        select(User).where(User.telegram_id == message.chat.id)
    )
    user = result.scalar_one_or_none()
    if user:
        user.flow_status = FlowStatus.VIDEO_SEEN
        await session.flush()
        await track_event(session, user.id, EventType.VIDEO_SEEN)

    await message.answer(
        "👇",
        reply_markup=generate_keyboard(lang),
    )


@router.callback_query(UserFlow.awaiting_video_action, F.data == "action:generate")
async def handle_generate_button(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """User pressed 'Generate Image' — check channel subscription first."""
    tg_id = callback.from_user.id
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()

    if user is None:
        await callback.answer("Please send /start first.")
        return

    lang = user.language.value if user.language else "ru"
    await callback.message.edit_reply_markup(reply_markup=None)

    # Delegate to subscription check
    from app.bot.handlers.subscription import send_subscription_prompt
    await send_subscription_prompt(callback.message, user, session, state)
    await callback.answer()
