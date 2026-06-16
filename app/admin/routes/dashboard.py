from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.auth import get_current_admin
from app.core.database import get_db_session
from app.services.analytics_service import get_dashboard_stats

router = APIRouter()
templates = Jinja2Templates(directory="app/admin/templates")


@router.get("/", response_class=HTMLResponse, response_model=None)
async def dashboard(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> HTMLResponse:
    stats = await get_dashboard_stats(session)
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "stats": stats, "active_page": "dashboard"},
    )
