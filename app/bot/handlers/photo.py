"""
Photo upload and image generation handler.

First generation:
  awaiting_photo  →  user sends selfie  →  _run_generation()

Regeneration (sequential, both steps optional):
  awaiting_regeneration_input
    → user presses «Generate new»
  awaiting_regen_photo  (step 1: new selfie or Skip)
    → photo received  →  store file_id in FSM  →  awaiting_regen_text
    → Skip            →  awaiting_regen_text (no_photo flag)
  awaiting_regen_text   (step 2: text description or Skip)
    → text received   →  store text  →  _run_generation()
    → Skip + had photo →  _run_generation() with stored photo, no text
    → Skip + no photo  →  «nothing changed» message, back to awaiting_regeneration_input
"""
import logging
import uuid
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, PhotoSize
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.builders import regenerate_keyboard, skip_keyboard
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
from app.services.image_service import (
    composite_into_frame_if_available,
    compress_to_webp,
    compress_user_photo,
)
from app.services.notify_service import notify_budget_exceeded, notify_new_image
from app.services.telegram_file_service import (
    download_telegram_file_bytes,
    purge_local_image_files,
    purge_user_photo_only,
    verify_telegram_file,
)

router = Router(name="photo")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

async def send_photo_request(
    message: Message,
    user: User,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """Ask the user to send a selfie (first generation)."""
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


# ---------------------------------------------------------------------------
# Budget check
# ---------------------------------------------------------------------------

async def _check_and_enforce_budget(
    session: AsyncSession,
    bot: Bot,
    user: User,
) -> bool:
    """
    Return True if generation is allowed, False if the budget cap is exceeded.
    Notifies the user and the admin when blocked.
    """
    limit_str = await settings_service.get(session, "budget_limit_usd") or "0"
    spent_str = await settings_service.get(session, "budget_spent_usd") or "0"
    limit = Decimal(limit_str)
    spent = Decimal(spent_str)

    if limit > 0 and spent >= limit:
        lang = user.language.value if user.language else "ru"
        exceeded_msg = await settings_service.get_text(session, "budget_exceeded_message", lang)
        await bot.send_message(chat_id=user.telegram_id, text=exceeded_msg)

        admin_id_str = await settings_service.get(session, "admin_telegram_user_id")
        if admin_id_str:
            try:
                await notify_budget_exceeded(bot, int(admin_id_str), float(limit), float(spent))
            except Exception:
                logger.exception("Failed to notify admin about budget exceeded")

        await track_event(session, user.id, EventType.BUDGET_EXCEEDED)
        return False

    return True


# ---------------------------------------------------------------------------
# Core generation pipeline
# ---------------------------------------------------------------------------

async def _run_generation(
    message: Message,
    bot: Bot,
    user: User,
    session: AsyncSession,
    state: FSMContext,
    user_photo_bytes: bytes,
    extra_prompt: str,
    *,
    is_regeneration: bool = False,
    user_photo_file_id: str | None = None,
) -> None:
    """AI call → frame/watermark → save → notify admin."""
    lang = user.language.value if user.language else "ru"

    if not await _check_and_enforce_budget(session, bot, user):
        return

    ambassador_path_rel = await settings_service.get(session, "ambassador_photo_path")
    if not ambassador_path_rel:
        logger.error("Ambassador photo not configured")
        await message.answer(
            "Generation is temporarily unavailable. Please try again later."
            if lang == "ru"
            else "Yaratish vaqtincha mavjud emas."
        )
        return

    ambassador_path = get_absolute_path(ambassador_path_rel)
    if not ambassador_path.exists():
        logger.error("Ambassador photo file not found: %s", ambassador_path)
        await message.answer(
            "Generation is temporarily unavailable."
            if lang == "ru"
            else "Vaqtincha mavjud emas."
        )
        return

    ambassador_bytes = ambassador_path.read_bytes()

    generating_text = await settings_service.get_text(session, "msg_generating", lang)
    generating_msg = await message.answer(generating_text)

    user.flow_status = FlowStatus.GENERATING
    attempt_number = user.regenerations_used + 1
    await session.flush()

    await track_event(session, user.id, EventType.GENERATION_REQUESTED)

    # Compress user selfie for temporary disk storage.
    # Original bytes are kept in-memory for AI — the stored copy is only a regen fallback.
    user_photo_webp = compress_user_photo(user_photo_bytes)
    user_photo_filename = generate_filename("user_photo.webp")
    user_photo_rel = await save_upload(user_photo_webp, "user_photos", user_photo_filename)

    image_record = GeneratedImage(
        id=str(uuid.uuid4()),
        user_id=user.id,
        status=ImageStatus.PENDING,
        attempt_number=attempt_number,
        user_photo_path=user_photo_rel,
        user_prompt_extra=extra_prompt or None,
        telegram_user_photo_file_id=user_photo_file_id,
    )
    session.add(image_record)
    await session.flush()

    # The AI generation uses user_photo_bytes from memory, not from disk.
    # If we already have a Telegram backup of the selfie (file_id from the upload),
    # the on-disk copy is redundant — remove it now to free space immediately.
    if image_record.telegram_user_photo_file_id:
        await purge_user_photo_only(image_record)
        await session.flush()

    try:
        prompt_template = await settings_service.get(session, "generation_prompt") or ""

        result = await generate_composite_photo(
            user_photo_bytes=user_photo_bytes,
            ambassador_photo_bytes=ambassador_bytes,
            prompt_template=prompt_template,
            extra_prompt=extra_prompt,
        )

        frame_path_rel = await settings_service.get(session, f"frame_path_{lang}")
        if frame_path_rel:
            final_image = composite_into_frame_if_available(result.image_bytes, frame_path_rel)
        else:
            # No frame — still compress to WebP for disk savings
            final_image = compress_to_webp(result.image_bytes)

        gen_filename = generate_filename("generated.webp")
        gen_rel_path = await save_upload(final_image, "generated", gen_filename)

        image_record.image_path = gen_rel_path
        image_record.input_tokens = result.input_tokens
        image_record.output_tokens = result.output_tokens
        image_record.cost_usd = float(result.cost_usd)
        await session.flush()

        await settings_service.add_budget_spent(session, result.cost_usd)

        # Check budget cap after spending (admin alert only)
        limit_str = await settings_service.get(session, "budget_limit_usd") or "0"
        spent_str = await settings_service.get(session, "budget_spent_usd") or "0"
        limit = Decimal(limit_str)
        spent = Decimal(spent_str)
        if limit > 0 and spent >= limit:
            admin_id_str = await settings_service.get(session, "admin_telegram_user_id")
            if admin_id_str:
                await notify_budget_exceeded(bot, int(admin_id_str), float(limit), float(spent))

        await track_event(session, user.id, EventType.IMAGE_GENERATED)

        user.flow_status = FlowStatus.AWAITING_APPROVAL
        user.regenerations_used = attempt_number
        await session.flush()

        admin_id_str = await settings_service.get(session, "admin_telegram_user_id")
        from app.core.config import get_settings
        config = get_settings()
        max_attempts_str = await settings_service.get(session, "max_regeneration_attempts") or "3"

        if admin_id_str:
            try:
                msg_id, chat_id, gen_file_id = await notify_new_image(
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
                if gen_file_id:
                    image_record.telegram_image_file_id = gen_file_id
                    # Verify the file is accessible then purge local copy immediately
                    if await verify_telegram_file(bot, gen_file_id):
                        await purge_local_image_files(image_record)
                        logger.info(
                            "Local files purged immediately after confirmed TG upload (image_id=%s)",
                            image_record.id,
                        )
                await session.flush()
            except Exception:
                logger.exception("Failed to send admin notification")

        await generating_msg.delete()
        pending_text = await settings_service.get_text(session, "msg_pending_review", lang)
        await message.answer(pending_text)

    except Exception:
        logger.exception("Image generation failed for user %d", user.telegram_id)
        image_record.status = ImageStatus.REJECTED
        user.regenerations_used = max(0, attempt_number - 1)
        user.flow_status = FlowStatus.VIDEO_SEEN
        await session.flush()

        try:
            await generating_msg.delete()
        except Exception:
            pass

        error_text = (
            "⚠️ Generation failed. Please try again."
            if lang == "ru"
            else "⚠️ Yaratish muvaffaqiyatsiz tugadi. Qayta urinib ko'ring."
        )
        if is_regeneration:
            user.flow_status = FlowStatus.DONE
            await session.flush()
            await state.set_state(UserFlow.awaiting_regeneration_input)
            await message.answer(error_text, reply_markup=regenerate_keyboard(lang))
        else:
            await state.set_state(UserFlow.awaiting_photo)
            await message.answer(
                error_text + "\n\n"
                + await settings_service.get_text(session, "msg_send_photo", lang)
            )


# ---------------------------------------------------------------------------
# First-generation handlers
# ---------------------------------------------------------------------------

@router.message(UserFlow.awaiting_photo, F.photo)
async def handle_photo_upload(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
) -> None:
    """Handle selfie photo from user (first attempt)."""
    tg_id = message.from_user.id
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if user is None:
        return

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
        extra_prompt="",
        user_photo_file_id=photo.file_id,
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


# ---------------------------------------------------------------------------
# Regeneration entry point
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "action:regenerate")
async def handle_regenerate_button(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """User pressed «Generate new» — begin sequential regen flow (step 1: photo).

    No FSM-state filter here: the 'Generate new' button may arrive after an app
    restart that wiped MemoryStorage.  We guard via DB checks instead.
    """
    tg_id = callback.from_user.id
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if user is None:
        await callback.answer()
        return

    lang = user.language.value if user.language else "ru"

    # Guard: user must have reached DONE state (result was delivered) and have remaining attempts
    if user.flow_status not in (FlowStatus.DONE,):
        await callback.answer()
        return

    max_attempts_str = await settings_service.get(session, "max_regeneration_attempts") or "3"
    max_attempts = int(max_attempts_str)
    attempts_remaining = max_attempts - user.regenerations_used

    if attempts_remaining <= 0:
        no_attempts_text = await settings_service.get_text(session, "msg_no_attempts_left", lang)
        await callback.answer(no_attempts_text[:200], show_alert=True)
        return

    # If user is mid-flow already, reset to avoid duplicate flows
    current_fsm = await state.get_state()
    if current_fsm in (
        UserFlow.awaiting_regen_photo.state,
        UserFlow.awaiting_regen_text.state,
    ):
        # Already in regen flow — reset and restart cleanly
        await state.clear()

    await callback.message.edit_reply_markup(reply_markup=None)

    # Clear any stale regen data
    await state.update_data(regen_photo_bytes=None, regen_extra_prompt=None, regen_user_photo_file_id=None)

    text = await settings_service.get_text(session, "msg_regen_ask_photo", lang)
    await callback.message.answer(text, reply_markup=skip_keyboard(lang))
    await state.set_state(UserFlow.awaiting_regen_photo)
    await callback.answer()


# ---------------------------------------------------------------------------
# Regeneration step 1: new photo (or Skip)
# ---------------------------------------------------------------------------

@router.message(UserFlow.awaiting_regen_photo, F.photo)
async def handle_regen_photo(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
) -> None:
    """User sent a new selfie — store it, proceed to step 2 (text)."""
    tg_id = message.from_user.id
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if user is None:
        return

    photo: PhotoSize = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    photo_bytes = await bot.download_file(file.file_path)
    photo_data = photo_bytes.read() if hasattr(photo_bytes, "read") else bytes(photo_bytes)

    await state.update_data(
        regen_photo_bytes=list(photo_data),
        regen_extra_prompt=None,
        regen_user_photo_file_id=photo.file_id,
    )

    lang = user.language.value if user.language else "ru"
    text = await settings_service.get_text(session, "msg_regen_ask_text", lang)
    await message.answer(text, reply_markup=skip_keyboard(lang))
    await state.set_state(UserFlow.awaiting_regen_text)


@router.callback_query(UserFlow.awaiting_regen_photo, F.data == "regen:skip")
async def handle_regen_photo_skip(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """User skipped the new photo step — proceed to step 2 (text)."""
    tg_id = callback.from_user.id
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if user is None:
        await callback.answer()
        return

    await state.update_data(regen_photo_bytes=None, regen_extra_prompt=None)
    await callback.message.edit_reply_markup(reply_markup=None)

    lang = user.language.value if user.language else "ru"
    text = await settings_service.get_text(session, "msg_regen_ask_text", lang)
    await callback.message.answer(text, reply_markup=skip_keyboard(lang))
    await state.set_state(UserFlow.awaiting_regen_text)
    await callback.answer()


@router.message(UserFlow.awaiting_regen_photo, ~F.photo)
async def handle_regen_photo_invalid(
    message: Message,
    session: AsyncSession,
) -> None:
    """User sent a non-photo message during the photo step — remind them."""
    tg_id = message.from_user.id
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    lang = user.language.value if user and user.language else "ru"
    text = await settings_service.get_text(session, "msg_invalid_photo", lang)
    await message.answer(text)


# ---------------------------------------------------------------------------
# Regeneration step 2: text description (or Skip)
# ---------------------------------------------------------------------------

@router.message(UserFlow.awaiting_regen_text, F.text)
async def handle_regen_text(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
) -> None:
    """User provided a text description — trigger generation."""
    tg_id = message.from_user.id
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if user is None:
        return

    data = await state.get_data()
    raw_photo = data.get("regen_photo_bytes")
    regen_user_photo_file_id = data.get("regen_user_photo_file_id")
    await state.update_data(regen_photo_bytes=None, regen_extra_prompt=None, regen_user_photo_file_id=None)

    await _run_regen(
        message=message,
        bot=bot,
        user=user,
        session=session,
        state=state,
        raw_photo=raw_photo,
        extra_prompt=message.text or "",
        regen_user_photo_file_id=regen_user_photo_file_id,
    )


@router.callback_query(UserFlow.awaiting_regen_text, F.data == "regen:skip")
async def handle_regen_text_skip(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
) -> None:
    """User skipped text step.

    - If they provided a photo in step 1 → run generation with no extra prompt.
    - If both steps were skipped → show «nothing changed» message.
    """
    tg_id = callback.from_user.id
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if user is None:
        await callback.answer()
        return

    data = await state.get_data()
    raw_photo = data.get("regen_photo_bytes")
    regen_user_photo_file_id = data.get("regen_user_photo_file_id")
    await state.update_data(regen_photo_bytes=None, regen_extra_prompt=None, regen_user_photo_file_id=None)
    await callback.message.edit_reply_markup(reply_markup=None)

    lang = user.language.value if user.language else "ru"

    if raw_photo is None:
        # Both steps skipped — nothing to regenerate
        nothing_text = await settings_service.get_text(
            session, "msg_regen_nothing_changed", lang
        )
        await callback.message.answer(
            nothing_text,
            reply_markup=regenerate_keyboard(lang),
        )
        await state.set_state(UserFlow.awaiting_regeneration_input)
        await callback.answer()
        return

    await callback.answer()
    await _run_regen(
        message=callback.message,
        bot=bot,
        user=user,
        session=session,
        state=state,
        raw_photo=raw_photo,
        extra_prompt="",
        regen_user_photo_file_id=regen_user_photo_file_id,
    )


# ---------------------------------------------------------------------------
# Internal: dispatch regen to core pipeline
# ---------------------------------------------------------------------------

async def _run_regen(
    message: Message,
    bot: Bot,
    user: User,
    session: AsyncSession,
    state: FSMContext,
    raw_photo: list[int] | None,
    extra_prompt: str,
    regen_user_photo_file_id: str | None = None,
) -> None:
    """
    Resolve the stored photo (or fall back to the ambassador photo alone)
    then call _run_generation.

    raw_photo is stored as list[int] in FSM memory (JSON-serialisable).
    If raw_photo is None, tries to reuse the previous selfie:
      1. Read from local disk (user_photo_path)
      2. Download from Telegram via telegram_user_photo_file_id
      3. Ask user to send a new photo
    """
    resolved_file_id: str | None = regen_user_photo_file_id

    if raw_photo is not None:
        photo_bytes = bytes(raw_photo)
    else:
        # No new photo provided — reuse the user's last submitted photo from DB
        from sqlalchemy import desc
        from app.models.generation import GeneratedImage as GI
        latest = await session.execute(
            select(GI)
            .where(GI.user_id == user.id)
            .order_by(desc(GI.created_at))
            .limit(1)
        )
        last_image = latest.scalar_one_or_none()
        lang = user.language.value if user.language else "ru"

        photo_bytes = None

        # 1. Try local disk copy
        if last_image and last_image.user_photo_path:
            photo_path = get_absolute_path(last_image.user_photo_path)
            if photo_path.exists():
                photo_bytes = photo_path.read_bytes()
                if not resolved_file_id:
                    resolved_file_id = last_image.telegram_user_photo_file_id

        # 2. Try downloading from Telegram
        if photo_bytes is None and last_image and last_image.telegram_user_photo_file_id:
            logger.info(
                "Local selfie purged; downloading from Telegram for regen (image_id=%s)",
                last_image.id,
            )
            photo_bytes = await download_telegram_file_bytes(
                bot, last_image.telegram_user_photo_file_id
            )
            if not resolved_file_id:
                resolved_file_id = last_image.telegram_user_photo_file_id

        # 3. Fail — ask user for new photo
        if photo_bytes is None:
            await message.answer(
                await settings_service.get_text(session, "msg_send_photo", lang)
            )
            await state.set_state(UserFlow.awaiting_photo)
            return

    await _run_generation(
        message=message,
        bot=bot,
        user=user,
        session=session,
        state=state,
        user_photo_bytes=photo_bytes,
        extra_prompt=extra_prompt,
        is_regeneration=True,
        user_photo_file_id=resolved_file_id,
    )
