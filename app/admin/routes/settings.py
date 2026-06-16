"""Admin settings routes — handles all configurable bot parameters."""
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.auth import get_current_admin
from app.core.database import get_db_session
from app.core.storage import generate_filename, save_upload
from app.services import settings_service

router = APIRouter()
templates = Jinja2Templates(directory="app/admin/templates")
logger = logging.getLogger(__name__)

# All message keys managed on the Messages tab
MESSAGE_KEYS = [
    "msg_welcome",
    "msg_privacy",
    "msg_video",
    "msg_subscribe",
    "msg_not_subscribed",
    "msg_send_photo",
    "msg_invalid_photo",
    "msg_generating",
    "msg_pending_review",
    "msg_approved",
    "msg_regenerate_prompt",
    "msg_already_participated",
    "msg_no_attempts_left",
    "rejection_message",
    "budget_exceeded_message",
]


@router.get("/", response_class=HTMLResponse)
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
            "label": k.replace("_", " ").replace("msg ", "").title(),
            "value_ru": all_settings.get(f"{k}_ru", ""),
            "value_uz": all_settings.get(f"{k}_uz", ""),
        }
        for k in MESSAGE_KEYS
    ]
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "s": all_settings,
            "message_pairs": message_pairs,
            "active_page": "settings",
            "active_tab": tab,
            "saved": request.query_params.get("saved"),
        },
    )


@router.post("/bot", response_class=RedirectResponse)
async def save_bot_settings(
    request: Request,
    bot_token: str = Form(""),
    telegram_channel_id: str = Form(""),
    admin_telegram_user_id: str = Form(""),
    privacy_policy_url: str = Form(""),
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> RedirectResponse:
    await settings_service.set_many(session, {
        "bot_token": bot_token.strip(),
        "telegram_channel_id": telegram_channel_id.strip(),
        "admin_telegram_user_id": admin_telegram_user_id.strip(),
        "privacy_policy_url": privacy_policy_url.strip(),
    })
    return RedirectResponse("/admin/settings?tab=bot&saved=1", status_code=303)


@router.post("/limits", response_class=RedirectResponse)
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


@router.post("/reset-budget", response_class=RedirectResponse)
async def reset_budget(
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> RedirectResponse:
    await settings_service.set(session, "budget_spent_usd", "0.000000")
    return RedirectResponse("/admin/settings?tab=limits&saved=1", status_code=303)


@router.post("/messages", response_class=RedirectResponse)
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


@router.post("/media", response_class=RedirectResponse)
async def save_media(
    request: Request,
    video_url_ru: str = Form(""),
    video_url_uz: str = Form(""),
    ambassador_photo: UploadFile | None = File(None),
    logo: UploadFile | None = File(None),
    video_file_ru: UploadFile | None = File(None),
    video_file_uz: UploadFile | None = File(None),
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> RedirectResponse:
    updates: dict[str, str] = {}

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

    if logo and logo.filename:
        data = await logo.read()
        if data:
            fn = generate_filename(logo.filename)
            rel = await save_upload(data, "logo", fn)
            updates["logo_path"] = rel

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

    if updates:
        await settings_service.set_many(session, updates)

    return RedirectResponse("/admin/settings?tab=media&saved=1", status_code=303)
