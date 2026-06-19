"""
Photo upload and image generation handler.

First generation:
  awaiting_photo        →  user sends main selfie
  awaiting_extra_photos →  collect up to MAX_EXTRA_PHOTOS additional angle photos (optional)
                        →  Skip / Done / max reached  →  _run_generation(photos_list)

Regeneration (sequential, all steps optional):
  awaiting_regeneration_input
    → user presses «Generate new»
  awaiting_regen_photo         (step 1: new main selfie or Skip)
    → photo received  →  store file_id  →  awaiting_regen_extra_photos
    → Skip            →  awaiting_regen_text (no new photo, skip extra-angles too)
  awaiting_regen_extra_photos  (step 1b: additional angle photos or Skip/Done)
    → photo / Skip / Done  →  awaiting_regen_text
  awaiting_regen_text          (step 2: text description or Skip)
    → text received   →  _run_generation()
    → Skip + had photo →  _run_generation() with stored photos, no text
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

from app.bot.keyboards.builders import (
    channel_keyboard,
    extra_photos_keyboard,
    regenerate_keyboard,
    share_bot_keyboard,
    skip_keyboard,
)
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
from app.services.telegram_file_service import download_telegram_file_bytes

router = Router(name="photo")
logger = logging.getLogger(__name__)

MAX_EXTRA_PHOTOS = 2  # max additional angle photos per generation (total = 1 main + 2 extras)


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
    max_attempts_str = await settings_service.get(session, "max_regeneration_attempts") or "3"
    attempts_remaining = int(max_attempts_str) - user.regenerations_used
    regen_key = "msg_regenerate_1left" if attempts_remaining == 1 else "msg_regenerate_2left"
    text = await settings_service.get_text(session, regen_key, lang)
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

        # Build channel link for "Go to channel" button.
        # Private channels (-100...) have no public t.me URL, so skip the button.
        channel_id = await settings_service.get(session, "telegram_channel_id") or ""
        if channel_id and not channel_id.startswith("-"):
            ch_link = f"https://t.me/{channel_id.lstrip('@')}"
            markup = channel_keyboard(lang, ch_link)
        else:
            markup = None

        await bot.send_message(chat_id=user.telegram_id, text=exceeded_msg, reply_markup=markup)

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
    user_photo_bytes_list: list[bytes],
    extra_prompt: str,
    *,
    is_regeneration: bool = False,
    user_photo_file_id: str | None = None,
) -> None:
    """AI call → frame/watermark → save → notify admin.

    user_photo_bytes_list: main selfie first, optional extra angles after.
    Only the first photo is persisted to disk (regen fallback); extras are ephemeral.
    """
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

    # Load the pre-cropped face region of the ambassador photo (best-effort).
    # The face crop gives Gemini a high-resolution close-up for better identity anchoring.
    # Convention: face crop lives in the same directory, named "ambassador_face_crop.<ext>".
    face_crop_path = ambassador_path.parent / ("ambassador_face_crop" + ambassador_path.suffix)
    ambassador_face_crop_bytes: bytes | None = None
    if face_crop_path.exists():
        ambassador_face_crop_bytes = face_crop_path.read_bytes()
    else:
        logger.warning("Ambassador face crop not found at %s; proceeding without it", face_crop_path)

    generating_text = await settings_service.get_text(session, "msg_generating", lang)
    generating_msg = await message.answer(generating_text)

    user.flow_status = FlowStatus.GENERATING
    attempt_number = user.regenerations_used + 1
    await session.flush()

    await track_event(session, user.id, EventType.GENERATION_REQUESTED)

    # Persist only the main (first) selfie to disk as a regen fallback.
    # Extra angle photos are used only for the AI call and are not stored.
    main_photo_bytes = user_photo_bytes_list[0]
    user_photo_webp = compress_user_photo(main_photo_bytes)
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

    try:
        prompt_template = await settings_service.get(session, "generation_prompt") or ""

        result = await generate_composite_photo(
            user_photo_bytes_list=user_photo_bytes_list,
            ambassador_photo_bytes=ambassador_bytes,
            prompt_template=prompt_template,
            extra_prompt=extra_prompt,
            ambassador_face_crop_bytes=ambassador_face_crop_bytes,
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
                await session.flush()
            except Exception:
                logger.exception("Failed to send admin notification")

        try:
            await generating_msg.delete()
        except Exception:
            pass
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
    """Handle main selfie from user (first attempt). Move to extra-photos collection."""
    tg_id = message.from_user.id
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if user is None:
        return

    photo: PhotoSize = message.photo[-1]
    # Store file_id only; bytes are downloaded right before generation to avoid OOM.
    await state.update_data(main_photo_file_id=photo.file_id, extra_photo_file_ids=[])

    lang = user.language.value if user.language else "ru"
    prompt_text = await settings_service.get_text(session, "msg_extra_photo_prompt", lang)
    await message.answer(prompt_text, reply_markup=extra_photos_keyboard(lang, has_photo=False))
    await state.set_state(UserFlow.awaiting_extra_photos)


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
# Extra photos collection (first generation)
# ---------------------------------------------------------------------------

async def _resolve_and_run_generation(
    message: Message,
    bot: Bot,
    user: User,
    session: AsyncSession,
    state: FSMContext,
    *,
    is_regeneration: bool = False,
) -> None:
    """Download all collected photos from Telegram and launch _run_generation."""
    data = await state.get_data()
    main_fid: str | None = data.get("main_photo_file_id")
    extra_fids: list[str] = data.get("extra_photo_file_ids") or []

    if main_fid is None:
        # Should not happen in normal flow — guard
        lang = user.language.value if user.language else "ru"
        await message.answer(await settings_service.get_text(session, "msg_send_photo", lang))
        await state.set_state(UserFlow.awaiting_photo)
        return

    main_bytes = await download_telegram_file_bytes(bot, main_fid)
    if main_bytes is None:
        lang = user.language.value if user.language else "ru"
        await message.answer(await settings_service.get_text(session, "msg_send_photo", lang))
        await state.set_state(UserFlow.awaiting_photo)
        return

    extras: list[bytes] = []
    for fid in extra_fids:
        b = await download_telegram_file_bytes(bot, fid)
        if b:
            extras.append(b)

    await _run_generation(
        message=message,
        bot=bot,
        user=user,
        session=session,
        state=state,
        user_photo_bytes_list=[main_bytes] + extras,
        extra_prompt="",
        is_regeneration=is_regeneration,
        user_photo_file_id=main_fid,
    )


@router.message(UserFlow.awaiting_extra_photos, F.photo)
async def handle_extra_photo(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
) -> None:
    """User sent an additional angle photo during the extra-photos step."""
    tg_id = message.from_user.id
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if user is None:
        return

    lang = user.language.value if user.language else "ru"
    data = await state.get_data()
    extra_fids: list[str] = list(data.get("extra_photo_file_ids") or [])

    photo: PhotoSize = message.photo[-1]
    extra_fids.append(photo.file_id)
    await state.update_data(extra_photo_file_ids=extra_fids)

    if len(extra_fids) >= MAX_EXTRA_PHOTOS:
        # Max reached — proceed to generation automatically
        await _resolve_and_run_generation(message, bot, user, session, state)
    else:
        n = len(extra_fids)  # already appended above, so count matches human expectation
        raw = await settings_service.get_text(session, "msg_extra_photo_added", lang)
        text = raw.format(n=n) if "{n}" in raw else raw
        await message.answer(text, reply_markup=extra_photos_keyboard(lang, has_photo=True))


@router.callback_query(UserFlow.awaiting_extra_photos, F.data.in_({"extra:skip", "extra:done"}))
async def handle_extra_photos_done(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
) -> None:
    """User pressed Skip or Done — proceed with photos collected so far."""
    tg_id = callback.from_user.id
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if user is None:
        await callback.answer()
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()
    await _resolve_and_run_generation(callback.message, bot, user, session, state)


@router.message(UserFlow.awaiting_extra_photos, ~F.photo)
async def handle_extra_photo_invalid(
    message: Message,
    session: AsyncSession,
) -> None:
    """User sent a non-photo message during extra-photos step."""
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

    # Guard: user must have received a result before regenerating
    if user.flow_status != FlowStatus.DONE:
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

    # Clear any stale regen data from a previous flow
    await state.update_data(regen_photo_file_id=None, regen_user_photo_file_id=None)

    text = await settings_service.get_text(session, "msg_regen_ask_your_photo", lang)
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
    """User sent a new main selfie — store it, offer extra angles (step 1b)."""
    tg_id = message.from_user.id
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if user is None:
        return

    photo: PhotoSize = message.photo[-1]

    # Store only the file_id to avoid OOM in MemoryStorage.
    await state.update_data(
        regen_photo_file_id=photo.file_id,
        regen_user_photo_file_id=photo.file_id,
        regen_extra_photo_file_ids=[],
    )

    lang = user.language.value if user.language else "ru"
    text = await settings_service.get_text(session, "msg_regen_ask_extra_photos", lang)
    await message.answer(text, reply_markup=extra_photos_keyboard(lang, has_photo=False))
    await state.set_state(UserFlow.awaiting_regen_extra_photos)


@router.callback_query(UserFlow.awaiting_regen_photo, F.data == "regen:skip")
async def handle_regen_photo_skip(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """User skipped the new photo step — skip extra angles too, go straight to text."""
    tg_id = callback.from_user.id
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if user is None:
        await callback.answer()
        return

    await state.update_data(
        regen_photo_file_id=None,
        regen_user_photo_file_id=None,
        regen_extra_photo_file_ids=[],
    )
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
# Regeneration step 1b: extra angle photos (or Skip/Done)
# ---------------------------------------------------------------------------

@router.message(UserFlow.awaiting_regen_extra_photos, F.photo)
async def handle_regen_extra_photo(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
) -> None:
    """User sent an extra-angle photo during regen — store and possibly advance."""
    tg_id = message.from_user.id
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if user is None:
        return

    lang = user.language.value if user.language else "ru"
    data = await state.get_data()
    extra_fids: list[str] = list(data.get("regen_extra_photo_file_ids") or [])

    photo: PhotoSize = message.photo[-1]
    extra_fids.append(photo.file_id)
    await state.update_data(regen_extra_photo_file_ids=extra_fids)

    if len(extra_fids) >= MAX_EXTRA_PHOTOS:
        # Max reached — move on to text step
        text = await settings_service.get_text(session, "msg_regen_ask_text", lang)
        await message.answer(text, reply_markup=skip_keyboard(lang))
        await state.set_state(UserFlow.awaiting_regen_text)
    else:
        n = len(extra_fids)  # already appended above
        raw = await settings_service.get_text(session, "msg_extra_photo_added", lang)
        text = raw.format(n=n) if "{n}" in raw else raw
        await message.answer(text, reply_markup=extra_photos_keyboard(lang, has_photo=True))


@router.callback_query(
    UserFlow.awaiting_regen_extra_photos,
    F.data.in_({"extra:skip", "extra:done"}),
)
async def handle_regen_extra_done(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """User is done with extra photos — proceed to text step."""
    tg_id = callback.from_user.id
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if user is None:
        await callback.answer()
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    lang = user.language.value if user.language else "ru"
    text = await settings_service.get_text(session, "msg_regen_ask_text", lang)
    await callback.message.answer(text, reply_markup=skip_keyboard(lang))
    await state.set_state(UserFlow.awaiting_regen_text)
    await callback.answer()


@router.message(UserFlow.awaiting_regen_extra_photos, ~F.photo)
async def handle_regen_extra_invalid(
    message: Message,
    session: AsyncSession,
) -> None:
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
    regen_photo_file_id = data.get("regen_photo_file_id")
    regen_user_photo_file_id = data.get("regen_user_photo_file_id")
    regen_extra_photo_file_ids = data.get("regen_extra_photo_file_ids") or []
    await state.update_data(
        regen_photo_file_id=None,
        regen_user_photo_file_id=None,
        regen_extra_photo_file_ids=[],
    )

    await _run_regen(
        message=message,
        bot=bot,
        user=user,
        session=session,
        state=state,
        regen_photo_file_id=regen_photo_file_id,
        extra_prompt=message.text or "",
        regen_user_photo_file_id=regen_user_photo_file_id,
        regen_extra_photo_file_ids=regen_extra_photo_file_ids,
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
    regen_photo_file_id = data.get("regen_photo_file_id")
    regen_user_photo_file_id = data.get("regen_user_photo_file_id")
    regen_extra_photo_file_ids = data.get("regen_extra_photo_file_ids") or []
    await state.update_data(
        regen_photo_file_id=None,
        regen_user_photo_file_id=None,
        regen_extra_photo_file_ids=[],
    )
    await callback.message.edit_reply_markup(reply_markup=None)

    lang = user.language.value if user.language else "ru"

    if regen_photo_file_id is None:
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
        regen_photo_file_id=regen_photo_file_id,
        extra_prompt="",
        regen_user_photo_file_id=regen_user_photo_file_id,
        regen_extra_photo_file_ids=regen_extra_photo_file_ids,
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
    regen_photo_file_id: str | None,
    extra_prompt: str,
    regen_user_photo_file_id: str | None = None,
    regen_extra_photo_file_ids: list[str] | None = None,
) -> None:
    """
    Resolve the selfies to use for regeneration then call _run_generation.

    Priority for the main selfie:
      1. regen_photo_file_id — user uploaded a NEW selfie in step 1; download it.
      2. DB fallback — user skipped step 1; reuse their last submitted selfie.
         a. Local disk copy (user_photo_path)
         b. Telegram download via telegram_user_photo_file_id
      3. Ask user to send a new photo (all sources exhausted).

    Extra angle photos (regen_extra_photo_file_ids) are downloaded if available.
    Photo bytes are never stored in FSM — only Telegram file_ids are kept.
    """
    lang = user.language.value if user.language else "ru"
    resolved_file_id: str | None = regen_user_photo_file_id
    main_photo_bytes: bytes | None = None

    if regen_photo_file_id is not None:
        logger.info("Downloading regen selfie from Telegram (file_id=%s)", regen_photo_file_id)
        main_photo_bytes = await download_telegram_file_bytes(bot, regen_photo_file_id)
        if main_photo_bytes is None:
            logger.warning("Could not download regen selfie; falling back to previous")
            regen_photo_file_id = None
        else:
            resolved_file_id = regen_photo_file_id

    if regen_photo_file_id is None:
        # No new photo — reuse the user's last submitted selfie from the DB
        from sqlalchemy import desc
        from app.models.generation import GeneratedImage as GI
        latest = await session.execute(
            select(GI)
            .where(GI.user_id == user.id)
            .order_by(desc(GI.created_at))
            .limit(1)
        )
        last_image = latest.scalar_one_or_none()

        # 2a. Try local disk copy
        if last_image and last_image.user_photo_path:
            photo_path = get_absolute_path(last_image.user_photo_path)
            if photo_path.exists():
                main_photo_bytes = photo_path.read_bytes()
                if not resolved_file_id:
                    resolved_file_id = last_image.telegram_user_photo_file_id

        # 2b. Local file missing — download from Telegram
        if main_photo_bytes is None and last_image and last_image.telegram_user_photo_file_id:
            logger.info(
                "Local selfie not on disk; downloading from Telegram for regen (image_id=%s)",
                last_image.id,
            )
            main_photo_bytes = await download_telegram_file_bytes(
                bot, last_image.telegram_user_photo_file_id
            )
            if not resolved_file_id:
                resolved_file_id = last_image.telegram_user_photo_file_id

        # 3. All sources exhausted — ask for a new photo
        if main_photo_bytes is None:
            await message.answer(
                await settings_service.get_text(session, "msg_send_photo", lang)
            )
            await state.set_state(UserFlow.awaiting_photo)
            return

    # Download extra angle photos (best-effort; failures are silently skipped)
    extra_bytes: list[bytes] = []
    for fid in (regen_extra_photo_file_ids or []):
        b = await download_telegram_file_bytes(bot, fid)
        if b:
            extra_bytes.append(b)

    await _run_generation(
        message=message,
        bot=bot,
        user=user,
        session=session,
        state=state,
        user_photo_bytes_list=[main_photo_bytes] + extra_bytes,
        extra_prompt=extra_prompt,
        is_regeneration=True,
        user_photo_file_id=resolved_file_id,
    )
