"""Service for managing blocked Telegram users."""
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.blocked_user import BlockedUser


def _normalize(username: str) -> str:
    """Normalize a Telegram username: strip whitespace, remove leading '@', lowercase."""
    return username.strip().lstrip("@").lower()


async def is_blocked(session: AsyncSession, username: str) -> bool:
    """Return True if the given Telegram username is blocked."""
    normalized = _normalize(username)
    if not normalized:
        return False
    result = await session.execute(
        select(BlockedUser).where(BlockedUser.username == normalized).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def add_blocked(session: AsyncSession, username: str) -> BlockedUser | None:
    """
    Add a username to the blocked list.

    Returns the created BlockedUser, or None if already blocked or username is empty.
    """
    normalized = _normalize(username)
    if not normalized:
        return None

    existing = await session.execute(
        select(BlockedUser).where(BlockedUser.username == normalized)
    )
    if existing.scalar_one_or_none() is not None:
        return None

    entry = BlockedUser(username=normalized)
    session.add(entry)
    await session.flush()
    return entry


async def remove_blocked(session: AsyncSession, username: str) -> bool:
    """Remove a username from the blocked list. Returns True if it existed."""
    normalized = _normalize(username)
    result = await session.execute(
        delete(BlockedUser).where(BlockedUser.username == normalized)
    )
    return result.rowcount > 0


async def list_blocked(session: AsyncSession) -> list[BlockedUser]:
    """Return all blocked users ordered by username."""
    result = await session.execute(
        select(BlockedUser).order_by(BlockedUser.username)
    )
    return list(result.scalars().all())
