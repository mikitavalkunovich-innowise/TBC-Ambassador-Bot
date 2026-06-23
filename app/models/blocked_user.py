"""Model for admin-managed blocked Telegram users."""
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class BlockedUser(Base, TimestampMixin):
    """
    Stores Telegram usernames that are silently blocked in the bot.

    Username is stored normalized: lowercase, without leading '@'.
    Users without a Telegram username cannot be blocked this way.
    """

    __tablename__ = "blocked_users"

    username: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
        index=True,
    )
