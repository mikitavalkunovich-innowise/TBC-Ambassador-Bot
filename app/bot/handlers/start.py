"""
/start handler.
- New user: begin flow with language selection.
- Existing user in progress: resume from last step.
- User who finished and has no attempts left: show "already participated" message.
"""
import logging

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.privacy import send_disclaimer
from app.bot.keyboards.builders import language_keyboard
from app.bot.states import UserFlow
from app.models.user import FlowStatus, User
from app.services import settings_service
from app.services.analytics_service import track_event
from app.models.event import EventType

router = Router(name="start")
logger = logging.getLogger(__name__)


async def _resume_user(message: Message, user: User, state: FSMContext, session: AsyncSession) -> None:
    """Resume an existing user's flow from their last saved status."""
    lang = user.language.value if user.language else "ru"

    match user.flow_status:
        case FlowStatus.STARTED:
            text = await settings_service.get(session, "msg_select_language_ru") or "Select language:"
            await message.answer(text, reply_markup=language_keyboard())
            await state.set_state(UserFlow.selecting_language)

        case FlowStatus.LANGUAGE_SET:
            await send_disclaimer(message, user, session, state)

        case FlowStatus.PRIVACY_ACCEPTED:
            # Re-send video step
            from app.bot.handlers.media import send_video_message
            await send_video_message(message, user, session)

        case FlowStatus.VIDEO_SEEN:
            # Re-show generate button
            from app.bot.handlers.media import send_generate_prompt
            await send_generate_prompt(message, lang, session)
            await state.set_state(UserFlow.awaiting_video_action)

        case FlowStatus.GENERATING | FlowStatus.AWAITING_APPROVAL:
            # Image is being processed / awaiting moderation
            pending_msg = await settings_service.get_text(session, "msg_pending_review", lang)
            await message.answer(pending_msg)

        case FlowStatus.DONE:
            # Check if regenerations are still available
            max_attempts_str = await settings_service.get(session, "max_regeneration_attempts") or "3"
            max_attempts = int(max_attempts_str)
            if user.regenerations_used < max_attempts:
                # Offer to regenerate
                from app.bot.handlers.photo import send_regenerate_prompt
                await send_regenerate_prompt(message, user, session, state)
            else:
                already_msg = await settings_service.get_text(session, "msg_already_participated", lang)
                await message.answer(already_msg)


@router.message(CommandStart())
async def handle_start(message: Message, state: FSMContext, session: AsyncSession) -> None:
    tg_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name

    # Look up existing user
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()

    if user is not None:
        # Resume existing user
        await _resume_user(message, user, state, session)
        return

    # New user — create record
    user = User(
        telegram_id=tg_id,
        telegram_username=username,
        telegram_first_name=first_name,
        flow_status=FlowStatus.STARTED,
    )
    session.add(user)
    await session.flush()  # Get the user.id assigned

    await track_event(session, user.id, EventType.STARTED)

    # Send language selection
    welcome_text = await settings_service.get(session, "msg_welcome_ru") or "Welcome!"
    select_lang_text = await settings_service.get(session, "msg_select_language_ru") or "Select language:"
    await message.answer(welcome_text)
    await message.answer(select_lang_text, reply_markup=language_keyboard())
    await state.set_state(UserFlow.selecting_language)
