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
    count_bot_blocked_users,
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
    bot_blocked_count = await count_bot_blocked_users(session)
    ru_count = await count_broadcast_recipients(session, "ru")
    uz_count = await count_broadcast_recipients(session, "uz")
    ru_missed_count = await count_broadcast_recipients(session, "ru", missed_only=True)
    uz_missed_count = await count_broadcast_recipients(session, "uz", missed_only=True)

    image_path_rel = await settings_service.get(session, "card_promo_image_path")
    image_path = resolve_card_promo_image_path(image_path_rel)
    preview_image_url = None
    if image_path_rel and image_path:
        preview_image_url = f"/media/{image_path_rel}"

    caption_ru = await settings_service.get_text(session, "msg_card_promo", "ru")
    caption_uz = await settings_service.get_text(session, "msg_card_promo", "uz")
    promo_enabled = await settings_service.get(session, "card_promo_enabled") == "1"
    click_tracking_enabled = (
        await settings_service.get(session, "card_promo_click_tracking_enabled", "1") == "1"
    )

    return templates.TemplateResponse(
        "promo.html",
        {
            "request": request,
            "active_page": "promo",
            "stats": stats,
            "recent": recent,
            "bot_blocked_count": bot_blocked_count,
            "ru_count": ru_count,
            "uz_count": uz_count,
            "ru_missed_count": ru_missed_count,
            "uz_missed_count": uz_missed_count,
            "preview_image_url": preview_image_url,
            "caption_ru": caption_ru,
            "caption_uz": caption_uz,
            "promo_enabled": promo_enabled,
            "click_tracking_enabled": click_tracking_enabled,
            "broadcast_started": request.query_params.get("broadcast_started"),
            "broadcast_lang": request.query_params.get("lang"),
            "broadcast_missed": request.query_params.get("missed"),
            "test_sent": request.query_params.get("test_sent"),
            "test_lang": request.query_params.get("test_lang"),
            "test_telegram_id": request.query_params.get("telegram_id", ""),
            "test_error": request.query_params.get("test_error"),
            "settings_saved": request.query_params.get("settings_saved"),
        },
    )


@router.post("/settings", response_model=None)
async def promo_settings(
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
    card_promo_click_tracking_enabled: str = Form("0"),
) -> RedirectResponse:
    await settings_service.set(
        session,
        "card_promo_click_tracking_enabled",
        "1" if card_promo_click_tracking_enabled == "1" else "0",
    )
    return RedirectResponse("/admin/promo?settings_saved=1", status_code=303)


async def _start_broadcast(
    request: Request,
    session: AsyncSession,
    language: str,
    *,
    missed_only: bool = False,
) -> RedirectResponse:
    """Launch background broadcast task and redirect immediately."""
    if await settings_service.get(session, "card_promo_enabled") != "1":
        return RedirectResponse("/admin/promo?error=promo_disabled", status_code=303)

    bot = getattr(request.app.state, "bot", None)
    if bot is None:
        return RedirectResponse("/admin/promo?error=no_bot", status_code=303)

    asyncio.create_task(
        broadcast_card_promo(
            async_session_factory,
            bot,
            language,
            missed_only=missed_only,
        )
    )
    mode = "missed-only" if missed_only else "all"
    logger.info("Card promo broadcast (%s, %s) task started", language, mode)
    missed_param = "&missed=1" if missed_only else ""
    return RedirectResponse(
        f"/admin/promo?broadcast_started=1&lang={language}{missed_param}",
        status_code=303,
    )


@router.post("/broadcast/ru", response_model=None)
async def broadcast_ru(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> RedirectResponse:
    return await _start_broadcast(request, session, "ru")


@router.post("/broadcast/uz", response_model=None)
async def broadcast_uz(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> RedirectResponse:
    return await _start_broadcast(request, session, "uz")


@router.post("/broadcast/missed/ru", response_model=None)
async def broadcast_missed_ru(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> RedirectResponse:
    return await _start_broadcast(request, session, "ru", missed_only=True)


@router.post("/broadcast/missed/uz", response_model=None)
async def broadcast_missed_uz(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> RedirectResponse:
    return await _start_broadcast(request, session, "uz", missed_only=True)


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

    if await settings_service.get(session, "card_promo_enabled") != "1":
        return _test_redirect(telegram_id=telegram_id_raw.strip(), test_error="promo_disabled")

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
