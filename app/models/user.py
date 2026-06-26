import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.event import AnalyticsEvent
    from app.models.generation import GeneratedImage


class Language(str, enum.Enum):
    RU = "ru"
    UZ = "uz"


class FlowStatus(str, enum.Enum):
    STARTED = "started"
    LANGUAGE_SET = "language_set"
    PRIVACY_ACCEPTED = "privacy_accepted"
    VIDEO_SEEN = "video_seen"
    GENERATING = "generating"
    AWAITING_APPROVAL = "awaiting_approval"
    DONE = "done"


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    telegram_first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    language: Mapped[Language | None] = mapped_column(
        Enum(Language, values_callable=lambda x: [e.value for e in x]),
        nullable=True,
    )
    privacy_accepted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    channel_subscribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    flow_status: Mapped[FlowStatus] = mapped_column(
        Enum(FlowStatus, values_callable=lambda x: [e.value for e in x]),
        default=FlowStatus.STARTED,
        nullable=False,
    )
    regenerations_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bot_blocked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # FSM state persistence (aiogram MemoryStorage fallback)
    fsm_state: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fsm_data: Mapped[str | None] = mapped_column(String(4096), nullable=True)

    images: Mapped[list["GeneratedImage"]] = relationship(
        "GeneratedImage", back_populates="user", lazy="select"
    )
    events: Mapped[list["AnalyticsEvent"]] = relationship(
        "AnalyticsEvent", back_populates="user", lazy="select"
    )
