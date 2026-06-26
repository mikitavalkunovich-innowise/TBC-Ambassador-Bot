"""Tracks card promo message deliveries and order-button clicks."""
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


class CardPromoSource:
    FLOW = "flow"
    BROADCAST = "broadcast"
    TEST = "test"


class CardPromoDelivery(Base, TimestampMixin):
    """One sent card promo message; click tracked via redirect URL."""

    __tablename__ = "card_promo_deliveries"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    language: Mapped[str] = mapped_column(String(2), nullable=False)
    clicked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    user: Mapped["User"] = relationship("User", lazy="select")
