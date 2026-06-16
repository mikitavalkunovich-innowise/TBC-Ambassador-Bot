"""
Photo upload and image generation handler.
Handles both first-time photo submission and regeneration requests.
"""
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, Message, PhotoSize
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.builders import regenerate_keyboard
from app.bot.states import UserFlow
from app.core.storage import (
    generate_filename,
    get_absolute_path,
    save_upload,
)
from app.models.event import EventType
from app.models.generation import GeneratedImage, ImageStatus
from app.models.user import FlowStatus, User
from app.services import settings_service
from app.services.ai_service import generate_composite_photo
from app.services.analytics_service import track_event
from app.services.image_service import add_logo_watermark_if_available
from app.services.notify_service import notify_budget_exceeded, notify_new_image

router = Router(name="photo")
logger = logging.getLogger(__name__)


async def send_photo_request(
    message: Message,
    user: User,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """Ask the user to send a selfie."""
    lang = user.language.value if user.language else "ru"
    text = await settings_service.get_text(session, "msg_send_photo", lang)
    await message.answer(text)
    await state.set_state(UserFlow.awaiting_photo)


async def send_regenerate_prompt(
    message: Message,
    user: User,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """Offer the user to generate a new photo (after approval or rejection)."""
    lang = user.language.value if user.language else "ru"
    text = await settings_service.get_text(session, "msg_regenerate_prompt", lang)
    await message.answer(text, reply_markup=regenerate_keyboard(lang))
    await state.set_state(UserFlow.awaiting_regeneration_input)


async def _check_and_enforce_budget(
    session: AsyncSession,
    bot: Bot,
    user: User,
) -> bool:
    """
    Check if budget limit is exceeded.
    Returns True if generation is allowed, False if blocked.
    Sends user notification and admin alert if blocked.
    """
    limit_str = await settings_service.get(session, "budget_limit_usd") or "0"
    spent_str = await settings_service.get(session, "budget_spent_usd") or "0"
    limit = Decimal(limit_str)
    spent = Decimal(spent_str)

    if limit > 0 and spent >= limit:
        lang = user.language.value if user.language else "ru"
        exceeded_msg = await settings_service.get_text(session, "budget_exceeded_message", lang)

        # Notify user
        from aiogram import Bot as AiogramBot
        await bot.send_message(chat_id=user.telegram_id, text=exceeded_msg)

        # Notify admin
        admin_id_str = await settings_service.get(session, "admin_telegram_user_id")
        if admin_id_str:
            try:
                from app.core.config import get_settings
                config = get_settings()
                await notify_budget_exceeded(bot, int(admin_id_str), float(limit), float(spent))
            except Exception:
                logger.exception("Failed to notify admin about budget exceeded")

        await track_event(session, user.id, EventType.BUDGET_EXCEEDED)
        return False

    return True


async def _run_generation(
    message: Message,
    bot: Bot,
    user: User,
    session: AsyncSession,
    state: FSMContext,
    user_photo_bytes: bytes,
    extra_prompt: str,
) -> None:
    """Core generation pipeline: AI call → watermark → save → notify admin."""
    lang = user.language.value if user.language else "ru"

    # Check budget
    if not await _check_and_enforce_budget(session, bot, user):
        return

    # Get ambassador photo
    ambassador_path_rel = await settings_service.get(session, "ambassador_photo_path")
    if not ambassador_path_rel:
        logger.error("Ambassador photo not configured")
        await message.answer(
            "Generation is temporarily unavailable. Please try again later." if lang == "ru"
            else "Yaratish vaqtincha mavjud emas."
        )
        return

    ambassador_path = get_absolute_path(ambassador_path_rel)
    if not ambassador_path.exists():
        logger.error("Ambassador photo file not found: %s", ambassador_path)
        await message.answer("Generation is temporarily unavailable." if lang == "ru" else "Vaqtincha mavjud emas.")
        return

    ambassador_bytes = ambassador_path.read_bytes()

    # Send "generating" message
    generating_text = await settings_service.get_text(session, "msg_generating", lang)
    generating_msg = await message.answer(generating_text)

    # Update user status
    user.flow_status = FlowStatus.GENERATING
    attempt_number = user.regenerations_used + 1
    await session.flush()

    await track_event(session, user.id, EventType.GENERATION_REQUESTED)

    # Save user photo
    user_photo_filename = generate_filename("user_photo.jpg")
    user_photo_rel = await save_upload(user_photo_bytes, "user_photos", user_photo_filename)

    # Create DB record for the generation
    image_record = GeneratedImage(
        id=str(uuid.uuid4()),
        user_id=user.id,
        status=ImageStatus.PENDING,
        attempt_number=attempt_number,
        user_photo_path=user_photo_rel,
        user_prompt_extra=extra_prompt or None,
    )
    session.add(image_record)
    await session.flush()

    try:
        # Get generation prompt
        prompt_template = await settings_service.get(session, "generation_prompt") or ""

        # Run AI generation
        result = await generate_composite_photo(
            user_photo_bytes=user_photo_bytes,
            ambassador_photo_bytes=ambassador_bytes,
            prompt_template=prompt_template,
            extra_prompt=extra_prompt,
        )

        # Apply watermark
        logo_path_rel = await settings_service.get(session, "logo_path")
        logo_abs = str(get_absolute_path(logo_path_rel)) if logo_path_rel else None
        final_image = add_logo_watermark_if_available(result.image_bytes, logo_abs)

        # Save generated image
        gen_filename = generate_filename("generated.jpg")
        gen_rel_path = await save_upload(final_image, "generated", gen_filename)

        # Update record with results
        image_record.image_path = gen_rel_path
        image_record.input_tokens = result.input_tokens
        image_record.output_tokens = result.output_tokens
        image_record.cost_usd = float(result.cost_usd)
        await session.flush()

        # Add cost to budget tracking
        await settings_service.add_budget_spent(session, result.cost_usd)

        # Re-check budget after spending (for alert only)
        limit_str = await settings_service.get(session, "budget_limit_usd") or "0"
        spent_str = await settings_service.get(session, "budget_spent_usd") or "0"
        limit = Decimal(limit_str)
        spent = Decimal(spent_str)
        if limit > 0 and spent >= limit:
            admin_id_str = await settings_service.get(session, "admin_telegram_user_id")
            if admin_id_str:
                await notify_budget_exceeded(bot, int(admin_id_str), float(limit), float(spent))

        await track_event(session, user.id, EventType.IMAGE_GENERATED)

        # Update user status
        user.flow_status = FlowStatus.AWAITING_APPROVAL
        user.regenerations_used = attempt_number
        await session.flush()

        # Notify admin for approval
        admin_id_str = await settings_service.get(session, "admin_telegram_user_id")
        from app.core.config import get_settings
        config = get_settings()
        max_attempts_str = await settings_service.get(session, "max_regeneration_attempts") or "3"

        if admin_id_str:
            try:
                msg_id, chat_id = await notify_new_image(
                    bot=bot,
                    admin_chat_id=int(admin_id_str),
                    image_id=image_record.id,
                    image_bytes=final_image,
                    telegram_id=user.telegram_id,
                    telegram_username=user.telegram_username,
                    language=lang,
                    attempt_number=attempt_number,
                    max_attempts=int(max_attempts_str),
                    admin_base_url=config.webhook_base_url,
                )
                image_record.admin_tg_message_id = msg_id
                image_record.admin_tg_chat_id = chat_id
                await session.flush()
            except Exception:
                logger.exception("Failed to send admin notification")

        # Inform user
        await generating_msg.delete()
        pending_text = await settings_service.get_text(session, "msg_pending_review", lang)
        await message.answer(pending_text)

    except Exception:
        logger.exception("Image generation failed for user %d", user.telegram_id)
        # Reset user status
        user.flow_status = FlowStatus.VIDEO_SEEN
        await session.flush()
        image_record.status = ImageStatus.REJECTED
        await session.flush()

        error_text = (
            "Generation failed. Please try again later."
            if lang == "ru"
            else "Yaratish muvaffaqiyatsiz tugadi. Keyinroq urinib ko'ring."
        )
        try:
            await generating_msg.delete()
        except Exception:
            pass
        await message.answer(error_text)


@router.message(UserFlow.awaiting_photo, F.photo)
async def handle_photo_upload(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
) -> None:
    """Handle selfie photo from user (first attempt or regeneration with optional extra prompt)."""
    tg_id = message.from_user.id
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()

    if user is None:
        return

    # Retrieve any extra prompt stored from a previous text message
    data = await state.get_data()
    extra_prompt = data.get("extra_prompt", "")
    if extra_prompt:
        await state.update_data(extra_prompt="")

    # Get highest resolution photo
    photo: PhotoSize = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    photo_bytes = await bot.download_file(file.file_path)
    photo_data = photo_bytes.read() if hasattr(photo_bytes, "read") else bytes(photo_bytes)

    await _run_generation(
        message=message,
        bot=bot,
        user=user,
        session=session,
        state=state,
        user_photo_bytes=photo_data,
        extra_prompt=extra_prompt,
    )


@router.message(UserFlow.awaiting_photo, ~F.photo)
async def handle_invalid_photo(
    message: Message,
    session: AsyncSession,
) -> None:
    """User sent something other than a photo."""
    tg_id = message.from_user.id
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    lang = user.language.value if user and user.language else "ru"

    text = await settings_service.get_text(session, "msg_invalid_photo", lang)
    await message.answer(text)


@router.callback_query(UserFlow.awaiting_regeneration_input, F.data == "action:regenerate")
async def handle_regenerate_button(
    callback: CallbackQuery,  # noqa: F821 (imported via aiogram.types)
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """User pressed 'Generate new' button — ask for new input."""
    tg_id = callback.from_user.id
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()

    if user is None:
        await callback.answer()
        return

    await state.set_state(UserFlow.awaiting_photo)
    await callback.message.edit_reply_markup(reply_markup=None)

    lang = user.language.value if user.language else "ru"
    text = await settings_service.get_text(session, "msg_send_photo", lang)
    await callback.message.answer(text)
    await callback.answer()


@router.message(UserFlow.awaiting_regeneration_input, F.text)
async def handle_regeneration_text(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
) -> None:
    """User sent a text prompt for regeneration — ask for new photo."""
    tg_id = message.from_user.id
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()

    if user is None:
        return

    lang = user.language.value if user.language else "ru"
    # Store extra prompt in FSM data
    await state.update_data(extra_prompt=message.text)
    await state.set_state(UserFlow.awaiting_photo)

    text = await settings_service.get_text(session, "msg_send_photo", lang)
    await message.answer(text)


# Import needed for callback_query type hint in the regenerate handler
from aiogram.types import CallbackQuery  # noqa: E402
