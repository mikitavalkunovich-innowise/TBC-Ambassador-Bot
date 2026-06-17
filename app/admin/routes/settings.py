"""Admin settings routes — handles all configurable bot parameters."""
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.auth import get_current_admin
from app.core.config import get_settings
from app.core.database import get_db_session
from app.core.storage import generate_filename, get_absolute_path, save_upload
from app.services import settings_service
from app.services.storage_service import cleanup_old_local_files, get_storage_stats
from app.services.user_service import reset_user_flow

router = APIRouter()
templates = Jinja2Templates(directory="app/admin/templates")
logger = logging.getLogger(__name__)

# Human-readable labels for the Messages tab (optional overrides)
MESSAGE_LABELS: dict[str, str] = {
    "msg_privacy": "Legal Disclaimer (text)",
    "btn_disclaimer_link": "Disclaimer — Read Policy button label",
    "btn_disclaimer_start": "Disclaimer — Start button label",
}

# All message keys managed on the Messages tab
MESSAGE_KEYS = [
    # --- Ordered by position in user flow ---
    "msg_welcome",
    "msg_privacy",
    "btn_disclaimer_link",
    "btn_disclaimer_start",
    "msg_subscribe",
    "msg_not_subscribed",
    "msg_send_photo",           # first photo request
    "msg_invalid_photo",
    "msg_generating",
    "msg_pending_review",
    "msg_approved",
    "msg_video",                # bonus video after approval
    "msg_regenerate_prompt",    # «Generate new» offer
    "msg_regen_ask_photo",      # regen step 1: new selfie or skip
    "msg_regen_ask_text",       # regen step 2: text or skip
    "msg_regen_nothing_changed",
    "msg_already_participated",
    "msg_no_attempts_left",
    "rejection_message",
    "budget_exceeded_message",
]


@router.get("/", response_class=HTMLResponse, response_model=None)
async def settings_page(
    request: Request,
    tab: str = "bot",
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> HTMLResponse:
    all_settings = await settings_service.get_all(session)
    message_pairs = [
        {
            "key": k,
            "label": MESSAGE_LABELS.get(k, k.replace("_", " ").replace("msg ", "").title()),
            "value_ru": all_settings.get(f"{k}_ru", ""),
            "value_uz": all_settings.get(f"{k}_uz", ""),
            "is_button": k.startswith("btn_"),
        }
        for k in MESSAGE_KEYS
    ]

    storage_stats = None
    if tab == "debug":
        try:
            storage_stats = await get_storage_stats(session)
        except Exception:
            logger.exception("Failed to load storage stats")

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "s": all_settings,
            "message_pairs": message_pairs,
            "active_page": "settings",
            "active_tab": tab,
            "saved": request.query_params.get("saved"),
            "storage_stats": storage_stats,
        },
    )


@router.post("/bot", response_class=RedirectResponse, response_model=None)
async def save_bot_settings(
    request: Request,
    bot_token: str = Form(""),
    telegram_channel_id: str = Form(""),
    admin_telegram_user_id: str = Form(""),
    privacy_policy_url: str = Form(""),
    privacy_policy_link_enabled: str = Form("0"),
    channel_check_enabled: str = Form("0"),
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> RedirectResponse:
    await settings_service.set_many(session, {
        "bot_token": bot_token.strip(),
        "telegram_channel_id": telegram_channel_id.strip(),
        "admin_telegram_user_id": admin_telegram_user_id.strip(),
        "privacy_policy_url": privacy_policy_url.strip(),
        "privacy_policy_link_enabled": "1" if privacy_policy_link_enabled == "1" else "0",
        "channel_check_enabled": "1" if channel_check_enabled == "1" else "0",
    })
    return RedirectResponse("/admin/settings?tab=bot&saved=1", status_code=303)


@router.post("/limits", response_class=RedirectResponse, response_model=None)
async def save_limits(
    max_regeneration_attempts: str = Form("3"),
    budget_limit_usd: str = Form("100.00"),
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> RedirectResponse:
    await settings_service.set_many(session, {
        "max_regeneration_attempts": max_regeneration_attempts.strip(),
        "budget_limit_usd": budget_limit_usd.strip(),
    })
    return RedirectResponse("/admin/settings?tab=limits&saved=1", status_code=303)


@router.post("/reset-budget", response_class=RedirectResponse, response_model=None)
async def reset_budget(
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> RedirectResponse:
    await settings_service.set(session, "budget_spent_usd", "0.000000")
    return RedirectResponse("/admin/settings?tab=limits&saved=1", status_code=303)


@router.post("/reset-user-flow", response_class=RedirectResponse, response_model=None)
async def reset_user_flow_route(
    telegram_user_id: str = Form(""),
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> RedirectResponse:
    raw = telegram_user_id.strip()
    if not raw.isdigit():
        return RedirectResponse("/admin/settings?tab=debug&error=invalid_user_id", status_code=303)

    telegram_id = int(raw)
    result = await reset_user_flow(session, telegram_id)
    if result is None:
        return RedirectResponse(
            f"/admin/settings?tab=debug&error=user_not_found&user_id={telegram_id}",
            status_code=303,
        )

    return RedirectResponse(
        "/admin/settings?tab=debug"
        f"&reset=ok&user_id={result.telegram_id}"
        f"&images={result.images_deleted}"
        f"&events={result.events_deleted}",
        status_code=303,
    )


@router.post("/cleanup-storage", response_class=RedirectResponse, response_model=None)
async def cleanup_storage_route(
    older_than_days: str = Form("2"),
    only_with_telegram_backup: str = Form("1"),
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> RedirectResponse:
    try:
        days = max(1, int(older_than_days.strip()))
    except ValueError:
        return RedirectResponse("/admin/settings?tab=debug&error=invalid_days", status_code=303)

    backup_only = only_with_telegram_backup == "1"
    result = await cleanup_old_local_files(session, days, only_with_telegram_backup=backup_only)

    return RedirectResponse(
        "/admin/settings?tab=debug"
        f"&cleanup=ok&purged={result.purged_count}"
        f"&mb={result.mb_freed}"
        f"&skipped={result.skipped_count}",
        status_code=303,
    )


@router.post("/messages", response_class=RedirectResponse, response_model=None)
async def save_messages(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> RedirectResponse:
    form_data = await request.form()
    updates: dict[str, str] = {}
    for key in MESSAGE_KEYS:
        for lang in ("ru", "uz"):
            field = f"{key}_{lang}"
            if field in form_data:
                updates[field] = str(form_data[field])
    # Also save generation prompt (single field, no language)
    if "generation_prompt" in form_data:
        updates["generation_prompt"] = str(form_data["generation_prompt"])
    await settings_service.set_many(session, updates)
    return RedirectResponse("/admin/settings?tab=messages&saved=1", status_code=303)


@router.post("/media", response_class=RedirectResponse, response_model=None)
async def save_media(
    request: Request,
    video_url_ru: str = Form(""),
    video_url_uz: str = Form(""),
    video_enabled: str = Form("0"),
    ambassador_photo: UploadFile | None = File(None),
    video_file_ru: UploadFile | None = File(None),
    video_file_uz: UploadFile | None = File(None),
    frame_ru: UploadFile | None = File(None),
    frame_uz: UploadFile | None = File(None),
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> RedirectResponse:
    updates: dict[str, str] = {
        "video_enabled": "1" if video_enabled == "1" else "0",
    }

    if video_url_ru.strip():
        updates["video_url_ru"] = video_url_ru.strip()
    if video_url_uz.strip():
        updates["video_url_uz"] = video_url_uz.strip()

    if ambassador_photo and ambassador_photo.filename:
        data = await ambassador_photo.read()
        if data:
            fn = generate_filename(ambassador_photo.filename)
            rel = await save_upload(data, "ambassador", fn)
            updates["ambassador_photo_path"] = rel

    if video_file_ru and video_file_ru.filename:
        data = await video_file_ru.read()
        if data:
            fn = generate_filename(video_file_ru.filename)
            rel = await save_upload(data, "videos", fn)
            updates["video_path_ru"] = rel
            updates["video_url_ru"] = ""  # file takes priority over URL

    if video_file_uz and video_file_uz.filename:
        data = await video_file_uz.read()
        if data:
            fn = generate_filename(video_file_uz.filename)
            rel = await save_upload(data, "videos", fn)
            updates["video_path_uz"] = rel
            updates["video_url_uz"] = ""

    # Instagram frame templates — stored as absolute paths in the upload volume
    if frame_ru and frame_ru.filename:
        data = await frame_ru.read()
        if data:
            fn = generate_filename(frame_ru.filename)
            rel = await save_upload(data, "frames", fn)
            # Store the absolute path so image_service can find it regardless
            # of the process working directory.
            updates["frame_path_ru"] = str(get_absolute_path(rel))

    if frame_uz and frame_uz.filename:
        data = await frame_uz.read()
        if data:
            fn = generate_filename(frame_uz.filename)
            rel = await save_upload(data, "frames", fn)
            updates["frame_path_uz"] = str(get_absolute_path(rel))

    if updates:
        await settings_service.set_many(session, updates)

    return RedirectResponse("/admin/settings?tab=media&saved=1", status_code=303)


@router.post("/reinit-bot", response_class=RedirectResponse, response_model=None)
async def reinit_bot(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> RedirectResponse:
    """Re-initialize the Telegram bot and re-register webhook without a full restart.

    If the Dispatcher already exists (bot was running before), only the Bot
    session is replaced so that registered aiogram routers are not re-attached
    (which would raise RuntimeError).  If the bot was never started, a full
    initialization is performed.
    """
    settings = get_settings()
    bot_token = await settings_service.get(session, "bot_token")

    if not bot_token:
        return RedirectResponse("/admin/settings?tab=bot&error=no_token", status_code=303)

    try:
        from app.bot.instance import initialize, reinit_bot_session, is_initialized

        if is_initialized():
            # Dispatcher already exists — only replace the Bot session.
            bot = await reinit_bot_session(bot_token)
            dp = request.app.state.dp  # keep existing dispatcher
        else:
            # First-time init: create both Bot and Dispatcher.
            bot, dp = await initialize(bot_token)
            request.app.state.dp = dp

        await bot.set_webhook(
            url=settings.webhook_url,
            drop_pending_updates=True,
        )
        logger.info("Webhook re-registered: %s", settings.webhook_url)
        request.app.state.bot = bot

    except Exception:
        logger.exception("Bot re-initialization failed")
        return RedirectResponse("/admin/settings?tab=bot&error=reinit_failed", status_code=303)

    return RedirectResponse("/admin/settings?tab=bot&saved=1", status_code=303)
