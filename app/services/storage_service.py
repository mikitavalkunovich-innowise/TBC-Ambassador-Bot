"""
Disk storage statistics and manual cleanup utilities for the admin Debug tab.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.services.telegram_file_service import purge_local_image_files

logger = logging.getLogger(__name__)


@dataclass
class CategoryStats:
    name: str
    file_count: int
    size_bytes: int

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / 1_048_576, 2)


@dataclass
class StorageStats:
    categories: list[CategoryStats]
    total_bytes: int
    records_with_local_files: int
    records_with_tg_backup_only: int

    @property
    def total_mb(self) -> float:
        return round(self.total_bytes / 1_048_576, 2)


@dataclass
class CleanupResult:
    purged_count: int
    skipped_count: int
    bytes_freed: int

    @property
    def mb_freed(self) -> float:
        return round(self.bytes_freed / 1_048_576, 2)


async def _dir_stats(path: Path) -> tuple[int, int]:
    """Return (file_count, total_bytes) for all files under path."""
    def _scan(p: Path) -> tuple[int, int]:
        count = 0
        total = 0
        if not p.exists():
            return 0, 0
        for f in p.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                    count += 1
                except OSError:
                    pass
        return count, total

    return await asyncio.to_thread(_scan, path)


async def get_storage_stats(session: AsyncSession) -> StorageStats:
    """
    Return disk usage per upload category and DB record counts.
    Non-blocking: all filesystem scanning runs in a thread pool.
    """
    from app.models.generation import GeneratedImage

    settings = get_settings()
    uploads = settings.uploads_path

    category_names = ["generated", "user_photos", "ambassador", "videos", "frames"]
    tasks = [_dir_stats(uploads / name) for name in category_names]
    results = await asyncio.gather(*tasks)

    categories = [
        CategoryStats(name=name, file_count=r[0], size_bytes=r[1])
        for name, r in zip(category_names, results)
    ]
    total_bytes = sum(c.size_bytes for c in categories)

    # DB counts
    rows = (await session.execute(
        select(
            GeneratedImage.image_path,
            GeneratedImage.user_photo_path,
            GeneratedImage.telegram_image_file_id,
            GeneratedImage.local_files_purged_at,
        )
    )).all()

    records_with_local_files = sum(
        1 for r in rows if (r.image_path or r.user_photo_path) and not r.local_files_purged_at
    )
    records_with_tg_backup_only = sum(
        1 for r in rows if r.telegram_image_file_id and r.local_files_purged_at
    )

    return StorageStats(
        categories=categories,
        total_bytes=total_bytes,
        records_with_local_files=records_with_local_files,
        records_with_tg_backup_only=records_with_tg_backup_only,
    )


async def cleanup_old_local_files(
    session: AsyncSession,
    older_than_days: int,
    only_with_telegram_backup: bool = True,
) -> CleanupResult:
    """
    Purge local disk copies for GeneratedImage records older than `older_than_days` days.

    If only_with_telegram_backup=True (recommended), skips records without a
    confirmed Telegram file_id so previews don't become completely unavailable.
    """
    from app.models.generation import GeneratedImage

    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)

    query = select(GeneratedImage).where(
        GeneratedImage.created_at < cutoff,
        GeneratedImage.local_files_purged_at.is_(None),
    )
    if only_with_telegram_backup:
        query = query.where(GeneratedImage.telegram_image_file_id.is_not(None))

    result = await session.execute(query)
    records = result.scalars().all()

    purged_count = 0
    skipped_count = 0
    total_freed = 0

    for record in records:
        if not record.image_path and not record.user_photo_path:
            skipped_count += 1
            continue
        freed = await purge_local_image_files(record)
        total_freed += freed
        purged_count += 1

    if purged_count:
        await session.commit()
        logger.info(
            "Manual cleanup: purged %d records, %.2f MB freed",
            purged_count,
            total_freed / 1_048_576,
        )

    return CleanupResult(
        purged_count=purged_count,
        skipped_count=skipped_count,
        bytes_freed=total_freed,
    )
