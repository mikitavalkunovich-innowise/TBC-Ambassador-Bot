from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


class EventType:
    STARTED = "started"
    LANGUAGE_SET = "language_set"
    PRIVACY_ACCEPTED = "privacy_accepted"
    VIDEO_SEEN = "video_seen"
    CHANNEL_SUBSCRIBED = "channel_subscribed"
    GENERATION_REQUESTED = "generation_requested"
    IMAGE_GENERATED = "image_generated"
    IMAGE_APPROVED = "image_approved"
    IMAGE_REJECTED = "image_rejected"
    FLOW_COMPLETED = "flow_completed"
    BUDGET_EXCEEDED = "budget_exceeded"


class AnalyticsEvent(Base, TimestampMixin):
    __tablename__ = "analytics_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    user: Mapped["User"] = relationship("User", back_populates="events")
