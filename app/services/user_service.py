"""User-related business logic."""
import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.storage import get_absolute_path
from app.models.event import AnalyticsEvent
from app.models.generation import GeneratedImage
from app.models.user import FlowStatus, User

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UserFlowResetResult:
    telegram_id: int
    images_deleted: int
    events_deleted: int


async def _delete_upload_file(relative_path: str | None) -> None:
    if not relative_path:
        return
    path = get_absolute_path(relative_path)
    if path.is_file():
        await asyncio.to_thread(path.unlink, missing_ok=True)


async def reset_user_flow(session: AsyncSession, telegram_id: int) -> UserFlowResetResult | None:
    """
    Reset a user's bot flow so they can start again from /start.

    Deletes generated images, user photos, and analytics events for the user.
    Does NOT change the global budget_spent_usd counter.
    """
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if user is None:
        return None

    images = (
        await session.execute(
            select(GeneratedImage).where(GeneratedImage.user_id == user.id)
        )
    ).scalars().all()

    for image in images:
        await _delete_upload_file(image.image_path)
        await _delete_upload_file(image.user_photo_path)

    images_deleted = len(images)

    events_result = await session.execute(
        delete(AnalyticsEvent).where(AnalyticsEvent.user_id == user.id)
    )
    await session.execute(delete(GeneratedImage).where(GeneratedImage.user_id == user.id))

    user.language = None
    user.privacy_accepted = False
    user.channel_subscribed_at = None
    user.flow_status = FlowStatus.STARTED
    user.regenerations_used = 0
    user.fsm_state = None
    user.fsm_data = None

    await session.flush()

    try:
        from app.bot.instance import clear_user_fsm

        await clear_user_fsm(telegram_id)
    except Exception:
        logger.warning("Could not clear in-memory FSM for user %d", telegram_id)

    events_deleted = events_result.rowcount or 0

    logger.info(
        "Reset user flow for telegram_id=%d (images=%d, events=%d); budget unchanged",
        telegram_id,
        images_deleted,
        events_deleted,
    )

    return UserFlowResetResult(
        telegram_id=telegram_id,
        images_deleted=images_deleted,
        events_deleted=events_deleted,
    )
