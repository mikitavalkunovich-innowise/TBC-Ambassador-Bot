"""Initial schema

Revision ID: 001
Revises:
Create Date: 2026-06-16

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- users ---
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_username", sa.String(255), nullable=True),
        sa.Column("telegram_first_name", sa.String(255), nullable=True),
        sa.Column(
            "language",
            sa.Enum("ru", "uz", name="language"),
            nullable=True,
        ),
        sa.Column("privacy_accepted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("channel_subscribed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "flow_status",
            sa.Enum(
                "started",
                "language_set",
                "privacy_accepted",
                "video_seen",
                "generating",
                "awaiting_approval",
                "done",
                name="flowstatus",
            ),
            nullable=False,
            server_default="started",
        ),
        sa.Column("regenerations_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fsm_state", sa.String(255), nullable=True),
        sa.Column("fsm_data", sa.String(4096), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_telegram_id", "users", ["telegram_id"], unique=True)

    # --- generated_images ---
    op.create_table(
        "generated_images",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("image_path", sa.String(512), nullable=True),
        sa.Column("user_photo_path", sa.String(512), nullable=True),
        sa.Column(
            "status",
            sa.Enum("pending", "approved", "rejected", name="imagestatus"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("user_prompt_extra", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=True),
        sa.Column("admin_tg_message_id", sa.BigInteger(), nullable=True),
        sa.Column("admin_tg_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_generated_images_user_id", "generated_images", ["user_id"])
    op.create_index("ix_generated_images_status", "generated_images", ["status"])

    # --- analytics_events ---
    op.create_table(
        "analytics_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_analytics_events_user_id", "analytics_events", ["user_id"])
    op.create_index("ix_analytics_events_event_type", "analytics_events", ["event_type"])

    # --- bot_settings ---
    op.create_table(
        "bot_settings",
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("bot_settings")
    op.drop_table("analytics_events")
    op.drop_table("generated_images")
    op.drop_table("users")
    op.execute("DROP TYPE IF EXISTS imagestatus")
    op.execute("DROP TYPE IF EXISTS flowstatus")
    op.execute("DROP TYPE IF EXISTS language")
