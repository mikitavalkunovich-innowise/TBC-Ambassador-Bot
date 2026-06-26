"""Admin Promo page — broadcast card promo and view click statistics."""
import asyncio
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.auth import get_current_admin
from app.core.database import async_session_factory, get_db_session
from app.services import settings_service
from app.services.card_promo_service import (
    broadcast_card_promo,
    count_broadcast_recipients,
    get_recent_deliveries,
    get_stats,
    resolve_card_promo_image_path,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/admin/templates")
logger = logging.getLogger(__name__)


@router.get("", response_class=HTMLResponse, response_model=None)
async def promo_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> HTMLResponse:
    stats = await get_stats(session)
    recent = await get_recent_deliveries(session, limit=50)
    ru_count = await count_broadcast_recipients(session, "ru")
    uz_count = await count_broadcast_recipients(session, "uz")

    image_path_rel = await settings_service.get(session, "card_promo_image_path")
    image_path = resolve_card_promo_image_path(image_path_rel)
    preview_image_url = None
    if image_path_rel and image_path:
        preview_image_url = f"/media/{image_path_rel}"

    caption_ru = await settings_service.get_text(session, "msg_card_promo", "ru")
    caption_uz = await settings_service.get_text(session, "msg_card_promo", "uz")
    promo_enabled = await settings_service.get(session, "card_promo_enabled") == "1"

    return templates.TemplateResponse(
        "promo.html",
        {
            "request": request,
            "active_page": "promo",
            "stats": stats,
            "recent": recent,
            "ru_count": ru_count,
            "uz_count": uz_count,
            "preview_image_url": preview_image_url,
            "caption_ru": caption_ru,
            "caption_uz": caption_uz,
            "promo_enabled": promo_enabled,
            "broadcast_started": request.query_params.get("broadcast_started"),
            "broadcast_lang": request.query_params.get("lang"),
        },
    )


def _start_broadcast(request: Request, language: str) -> RedirectResponse:
    """Launch background broadcast task and redirect immediately."""
    bot = getattr(request.app.state, "bot", None)
    if bot is None:
        return RedirectResponse("/admin/promo?error=no_bot", status_code=303)

    asyncio.create_task(
        broadcast_card_promo(async_session_factory, bot, language)
    )
    logger.info("Card promo broadcast (%s) task started", language)
    return RedirectResponse(
        f"/admin/promo?broadcast_started=1&lang={language}",
        status_code=303,
    )


@router.post("/broadcast/ru", response_model=None)
async def broadcast_ru(
    request: Request,
    _admin: str = Depends(get_current_admin),
) -> RedirectResponse:
    return _start_broadcast(request, "ru")


@router.post("/broadcast/uz", response_model=None)
async def broadcast_uz(
    request: Request,
    _admin: str = Depends(get_current_admin),
) -> RedirectResponse:
    return _start_broadcast(request, "uz")
