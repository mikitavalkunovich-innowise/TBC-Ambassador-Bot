"""
Utilities for Telegram file management.

Downloads file bytes by file_id (used as regen fallback when the local selfie
is no longer on disk) and purges local disk copies once a Telegram backup is
confirmed.
"""
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from aiogram import Bot

from app.core.storage import get_absolute_path

if TYPE_CHECKING:
    from app.models.generation import GeneratedImage

logger = logging.getLogger(__name__)


async def download_telegram_file_bytes(bot: Bot, file_id: str) -> bytes | None:
    """
    Download file bytes from Telegram by file_id.
    Used as fallback when the local selfie has been purged but regen is requested.
    Returns None on any error.
    """
    try:
        tg_file = await bot.get_file(file_id)
        if not tg_file.file_path:
            return None
        token = bot.token
        url = f"https://api.telegram.org/file/bot{token}/{tg_file.file_path}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content
    except Exception:
        logger.warning("Could not download Telegram file file_id=%s", file_id)
        return None


async def _unlink_if_exists(relative_path: str | None) -> int:
    """
    Delete a file from the uploads directory. Returns bytes freed (0 if not found).
    All filesystem calls are run in a thread pool to avoid blocking the event loop.
    """
    if not relative_path:
        return 0

    path = get_absolute_path(relative_path)

    def _stat_and_unlink(p: Path) -> int:
        if not p.is_file():
            return 0
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        p.unlink(missing_ok=True)
        return size

    return await asyncio.to_thread(_stat_and_unlink, path)


async def purge_local_image_files(record: "GeneratedImage") -> int:
    """
    Delete local disk copies of the generated image and user photo.

    Sets local_files_purged_at and clears image_path / user_photo_path on the
    record object (caller must flush/commit the session).

    Each file is deleted independently — an error on one does not prevent
    the other from being removed.

    Returns total bytes freed.
    """
    freed = 0

    if record.image_path:
        try:
            freed += await _unlink_if_exists(record.image_path)
        except Exception:
            logger.warning("Failed to delete image file: %s", record.image_path)
        record.image_path = None

    if record.user_photo_path:
        try:
            freed += await _unlink_if_exists(record.user_photo_path)
        except Exception:
            logger.warning("Failed to delete user photo file: %s", record.user_photo_path)
        record.user_photo_path = None

    record.local_files_purged_at = datetime.now(timezone.utc)

    logger.debug(
        "Purged local files for image_id=%s (freed %d bytes)", record.id, freed
    )
    return freed
