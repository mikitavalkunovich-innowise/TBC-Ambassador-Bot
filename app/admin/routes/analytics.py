"""Analytics and CSV export routes."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.auth import get_current_admin
from app.core.database import get_db_session
from app.services.analytics_service import export_csv, get_dashboard_stats

router = APIRouter()
templates = Jinja2Templates(directory="app/admin/templates")


@router.get("/export/csv", response_model=None)
async def export_analytics_csv(
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> Response:
    csv_bytes = await export_csv(session)
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=tbc_analytics.csv"},
    )
