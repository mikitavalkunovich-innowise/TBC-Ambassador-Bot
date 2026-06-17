from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.auth import get_current_admin
from app.core.database import get_db_session
from app.services.analytics_service import get_dashboard_stats
from app.services import settings_service

router = APIRouter()
templates = Jinja2Templates(directory="app/admin/templates")


def _build_channel_link(channel_id: str) -> str | None:
    """Build a t.me link from a channel username or public ID."""
    channel_id = channel_id.strip()
    if not channel_id:
        return None
    if channel_id.startswith("-100"):
        return None  # private channel — no public link
    username = channel_id.lstrip("@")
    return f"https://t.me/{username}"


@router.get("/", response_class=HTMLResponse, response_model=None)
async def dashboard(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> HTMLResponse:
    stats = await get_dashboard_stats(session)

    channel_id = await settings_service.get(session, "telegram_channel_id") or ""
    channel_link = _build_channel_link(channel_id)
    channel_display = channel_id or None

    bot_link: str | None = None
    bot_username: str | None = None
    try:
        from app.bot.instance import get_bot, is_initialized

        if is_initialized():
            me = await get_bot().get_me()
            if me.username:
                bot_username = me.username
                bot_link = f"https://t.me/{me.username}"
    except Exception:
        pass

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "stats": stats,
            "active_page": "dashboard",
            "bot_link": bot_link,
            "bot_username": bot_username,
            "channel_link": channel_link,
            "channel_display": channel_display,
        },
    )
