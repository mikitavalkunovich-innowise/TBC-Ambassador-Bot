import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


class ImageStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class GeneratedImage(Base, TimestampMixin):
    __tablename__ = "generated_images"

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

    # File paths (relative to UPLOADS_DIR)
    image_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    user_photo_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    status: Mapped[ImageStatus] = mapped_column(
        Enum(ImageStatus, values_callable=lambda x: [e.value for e in x]),
        default=ImageStatus.PENDING,
        nullable=False,
        index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    user_prompt_extra: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Cost tracking
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)

    # Telegram message for inline approval
    admin_tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    admin_tg_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship("User", back_populates="images")
