"""Analytics service: event tracking and stats aggregation."""
import csv
import io
import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import AnalyticsEvent, EventType
from app.models.generation import GeneratedImage, ImageStatus
from app.models.setting import BotSetting
from app.models.user import FlowStatus, User

logger = logging.getLogger(__name__)


async def track_event(session: AsyncSession, user_id: int, event_type: str) -> None:
    """Record an analytics event for the given user."""
    session.add(AnalyticsEvent(user_id=user_id, event_type=event_type))
    await session.commit()


async def get_dashboard_stats(session: AsyncSession) -> dict:
    """Aggregate stats for the admin dashboard."""
    total_users = (await session.execute(select(func.count(User.id)))).scalar_one()

    users_completed = (
        await session.execute(
            select(func.count(User.id)).where(User.flow_status == FlowStatus.DONE)
        )
    ).scalar_one()

    images_generated = (
        await session.execute(select(func.count(GeneratedImage.id)))
    ).scalar_one()

    images_pending = (
        await session.execute(
            select(func.count(GeneratedImage.id)).where(
                GeneratedImage.status == ImageStatus.PENDING
            )
        )
    ).scalar_one()

    images_approved = (
        await session.execute(
            select(func.count(GeneratedImage.id)).where(
                GeneratedImage.status == ImageStatus.APPROVED
            )
        )
    ).scalar_one()

    images_rejected = (
        await session.execute(
            select(func.count(GeneratedImage.id)).where(
                GeneratedImage.status == ImageStatus.REJECTED
            )
        )
    ).scalar_one()

    total_cost_row = (
        await session.execute(select(func.sum(GeneratedImage.cost_usd)))
    ).scalar_one()
    total_cost = float(total_cost_row or 0)

    # Budget info from settings
    budget_limit_row = await session.get(BotSetting, "budget_limit_usd")
    budget_spent_row = await session.get(BotSetting, "budget_spent_usd")
    budget_limit = float(budget_limit_row.value or "0") if budget_limit_row else 0.0
    budget_spent = float(budget_spent_row.value or "0") if budget_spent_row else 0.0

    # Language distribution
    ru_users = (
        await session.execute(
            select(func.count(User.id)).where(User.language == "ru")
        )
    ).scalar_one()
    uz_users = (
        await session.execute(
            select(func.count(User.id)).where(User.language == "uz")
        )
    ).scalar_one()

    return {
        "total_users": total_users,
        "users_completed": users_completed,
        "images_generated": images_generated,
        "images_pending": images_pending,
        "images_approved": images_approved,
        "images_rejected": images_rejected,
        "total_cost_usd": round(total_cost, 4),
        "budget_limit_usd": budget_limit,
        "budget_spent_usd": budget_spent,
        "budget_remaining_usd": round(max(0.0, budget_limit - budget_spent), 4),
        "budget_percent": round((budget_spent / budget_limit * 100) if budget_limit > 0 else 0, 1),
        "users_ru": ru_users,
        "users_uz": uz_users,
    }


async def export_csv(session: AsyncSession) -> bytes:
    """Export all analytics events as a CSV file."""
    rows = await session.execute(
        select(
            AnalyticsEvent.id,
            AnalyticsEvent.event_type,
            AnalyticsEvent.created_at,
            User.telegram_id,
            User.telegram_username,
            User.language,
            User.flow_status,
        )
        .join(User, AnalyticsEvent.user_id == User.id)
        .order_by(AnalyticsEvent.created_at.desc())
    )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "event_id", "event_type", "created_at",
        "telegram_id", "telegram_username", "language", "flow_status",
    ])
    for row in rows:
        writer.writerow([
            row.id,
            row.event_type,
            row.created_at.isoformat() if row.created_at else "",
            row.telegram_id,
            row.telegram_username or "",
            row.language or "",
            row.flow_status or "",
        ])

    return buf.getvalue().encode("utf-8")
