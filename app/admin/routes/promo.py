"""Admin Promo page — broadcast card promo and view click statistics."""
import asyncio
import logging

from fastapi import APIRouter, Depends, Form, Request
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
    send_card_promo_test,
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
            "test_sent": request.query_params.get("test_sent"),
            "test_lang": request.query_params.get("test_lang"),
            "test_telegram_id": request.query_params.get("telegram_id", ""),
            "test_error": request.query_params.get("test_error"),
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


def _test_redirect(
    *,
    telegram_id: str,
    test_sent: bool = False,
    test_lang: str | None = None,
    test_error: str | None = None,
) -> RedirectResponse:
    params = [f"telegram_id={telegram_id}"]
    if test_sent:
        params.append("test_sent=1")
        if test_lang:
            params.append(f"test_lang={test_lang}")
    if test_error:
        params.append(f"test_error={test_error}")
    return RedirectResponse(f"/admin/promo?{'&'.join(params)}", status_code=303)


async def _send_test_promo(
    request: Request,
    session: AsyncSession,
    language: str,
    telegram_id_raw: str,
) -> RedirectResponse:
    bot = getattr(request.app.state, "bot", None)
    if bot is None:
        return _test_redirect(telegram_id=telegram_id_raw, test_error="no_bot")

    raw = telegram_id_raw.strip()
    if not raw:
        return _test_redirect(telegram_id="", test_error="missing_id")

    try:
        telegram_id = int(raw)
    except ValueError:
        return _test_redirect(telegram_id=raw, test_error="invalid_id")

    try:
        await send_card_promo_test(bot, session, telegram_id, language)
        await session.commit()
    except ValueError as exc:
        await session.rollback()
        return _test_redirect(
            telegram_id=str(telegram_id),
            test_error=str(exc),
        )
    except Exception:
        await session.rollback()
        logger.exception("Card promo test send failed for telegram_id=%d", telegram_id)
        return _test_redirect(
            telegram_id=str(telegram_id),
            test_error="send_failed",
        )

    logger.info("Card promo test (%s) sent to telegram_id=%d", language, telegram_id)
    return _test_redirect(
        telegram_id=str(telegram_id),
        test_sent=True,
        test_lang=language,
    )


@router.post("/test/ru", response_model=None)
async def test_ru(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
    telegram_id: str = Form(""),
) -> RedirectResponse:
    return await _send_test_promo(request, session, "ru", telegram_id)


@router.post("/test/uz", response_model=None)
async def test_uz(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
    telegram_id: str = Form(""),
) -> RedirectResponse:
    return await _send_test_promo(request, session, "uz", telegram_id)
