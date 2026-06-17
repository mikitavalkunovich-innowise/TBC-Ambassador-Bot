"""
Utilities for Telegram file_id management.

Captures file_ids from send_photo responses, verifies file accessibility,
downloads file bytes for reuse (e.g. regen without local selfie), and
purges local disk copies once a Telegram backup is confirmed.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx
from aiogram import Bot
from aiogram.types import Message

from app.core.storage import get_absolute_path

if TYPE_CHECKING:
    from app.models.generation import GeneratedImage

logger = logging.getLogger(__name__)


def capture_photo_file_id(msg: Message) -> str | None:
    """Extract the file_id of the largest photo from a send_photo() response."""
    if msg.photo:
        return msg.photo[-1].file_id
    return None


async def verify_telegram_file(bot: Bot, file_id: str) -> bool:
    """
    Call getFile to confirm the file is still accessible on Telegram servers.
    Returns True if file_path is returned successfully.
    """
    try:
        tg_file = await bot.get_file(file_id)
        return bool(tg_file.file_path)
    except Exception:
        logger.warning("Could not verify Telegram file_id=%s", file_id)
        return False


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
    """Delete a file from the uploads directory. Returns bytes freed (0 if not found)."""
    if not relative_path:
        return 0
    path = get_absolute_path(relative_path)
    if not path.is_file():
        return 0
    size = await asyncio.to_thread(path.stat)
    size_bytes = size.st_size
    await asyncio.to_thread(path.unlink, missing_ok=True)
    return size_bytes


async def purge_local_image_files(record: "GeneratedImage") -> int:
    """
    Delete local disk copies of the generated image and user photo.

    Sets local_files_purged_at and clears image_path / user_photo_path on the
    record object (caller must flush/commit the session).

    Returns total bytes freed.
    """
    freed = 0
    freed += await _unlink_if_exists(record.image_path)
    freed += await _unlink_if_exists(record.user_photo_path)

    record.image_path = None
    record.user_photo_path = None
    record.local_files_purged_at = datetime.now(timezone.utc)

    logger.debug(
        "Purged local files for image_id=%s (freed %d bytes)", record.id, freed
    )
    return freed
