"""
Background task: automatically purge local image files older than 48 hours
when a Telegram backup (file_id) exists.

Runs in an infinite asyncio loop, sleeping 48 h between passes.
Local disk is cleaned, but all database metadata (status, cost, user info,
telegram_image_file_id) is preserved so the admin panel continues to work.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.telegram_file_service import purge_local_image_files

logger = logging.getLogger(__name__)

PURGE_AFTER_HOURS: int = 48


async def purge_old_local_files(session: AsyncSession, older_than_hours: int = PURGE_AFTER_HOURS) -> dict:
    """
    Delete local disk copies of generated images and user photos for records:
      - created more than `older_than_hours` ago
      - that have a confirmed Telegram backup (telegram_image_file_id IS NOT NULL)
      - that have not been purged yet (local_files_purged_at IS NULL)

    Returns a summary dict with purged count and total bytes freed.
    """
    from app.models.generation import GeneratedImage

    cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)

    result = await session.execute(
        select(GeneratedImage).where(
            GeneratedImage.created_at < cutoff,
            GeneratedImage.telegram_image_file_id.is_not(None),
            GeneratedImage.local_files_purged_at.is_(None),
        )
    )
    records = result.scalars().all()

    total_freed = 0
    purged_count = 0
    for record in records:
        freed = await purge_local_image_files(record)
        total_freed += freed
        purged_count += 1

    if purged_count:
        await session.commit()
        logger.info(
            "Auto-purge: removed local files for %d images (%.2f MB freed)",
            purged_count,
            total_freed / 1_048_576,
        )
    else:
        logger.debug("Auto-purge: nothing to purge (checked %d records)", len(records))

    return {"purged": purged_count, "bytes_freed": total_freed}


async def run_purge_loop(session_factory) -> None:
    """
    Infinite loop that sleeps 48 hours then runs purge_old_local_files.
    Designed to be launched as an asyncio background task at startup.
    Exceptions are caught per iteration so the loop never dies silently.
    """
    logger.info("Auto-purge task started (interval=%dh)", PURGE_AFTER_HOURS)
    while True:
        await asyncio.sleep(PURGE_AFTER_HOURS * 3600)
        try:
            async with session_factory() as session:
                summary = await purge_old_local_files(session, PURGE_AFTER_HOURS)
                logger.info("Auto-purge pass complete: %s", summary)
        except Exception:
            logger.exception("Auto-purge task encountered an error; will retry next cycle")
