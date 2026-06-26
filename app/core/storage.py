import uuid
from pathlib import Path

import aiofiles

from app.core.config import get_settings

settings = get_settings()

SUBDIRS = {
    "ambassador": "ambassador",
    "logo": "logo",
    "videos": "videos",
    "generated": "generated",
    "user_photos": "user_photos",
    "frames": "frames",
    "debug": "debug",
    "card_promo": "card_promo",
}


def ensure_dirs() -> None:
    """Create all upload subdirectories if they don't exist."""
    for subdir in SUBDIRS.values():
        (settings.uploads_path / subdir).mkdir(parents=True, exist_ok=True)


def get_upload_path(category: str, filename: str) -> Path:
    """Return the absolute path for a file in the given category."""
    return settings.uploads_path / SUBDIRS[category] / filename


def generate_filename(original_name: str) -> str:
    """Generate a unique filename preserving the original extension."""
    suffix = Path(original_name).suffix.lower()
    return f"{uuid.uuid4().hex}{suffix}"


async def save_upload(data: bytes, category: str, filename: str | None = None) -> str:
    """
    Save bytes to the uploads directory.
    Returns the relative path (category/filename) for DB storage.
    """
    if filename is None:
        filename = f"{uuid.uuid4().hex}.bin"

    ensure_dirs()
    full_path = get_upload_path(category, filename)

    async with aiofiles.open(full_path, "wb") as f:
        await f.write(data)

    return f"{SUBDIRS[category]}/{filename}"


async def read_upload(relative_path: str) -> bytes:
    """Read a file from the uploads directory by its relative path."""
    full_path = settings.uploads_path / relative_path
    async with aiofiles.open(full_path, "rb") as f:
        return await f.read()


def get_absolute_path(relative_path: str) -> Path:
    """Resolve a relative upload path to an absolute Path."""
    return settings.uploads_path / relative_path


def relative_to_url(relative_path: str) -> str:
    """Convert a relative upload path to the media URL served by FastAPI."""
    return f"/media/{relative_path}"
