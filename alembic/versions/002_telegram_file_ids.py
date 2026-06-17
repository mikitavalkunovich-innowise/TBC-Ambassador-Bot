"""Add Telegram file_id fields and local_files_purged_at to generated_images

Revision ID: 002
Revises: 001
Create Date: 2026-06-17

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "generated_images",
        sa.Column("telegram_image_file_id", sa.String(256), nullable=True),
    )
    op.add_column(
        "generated_images",
        sa.Column("telegram_user_photo_file_id", sa.String(256), nullable=True),
    )
    op.add_column(
        "generated_images",
        sa.Column("local_files_purged_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("generated_images", "local_files_purged_at")
    op.drop_column("generated_images", "telegram_user_photo_file_id")
    op.drop_column("generated_images", "telegram_image_file_id")
