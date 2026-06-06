"""Add ios_push_tokens table

Revision ID: add_ios_push_tokens
Revises: backfill_email_attachment_ids
Create Date: 2026-06-06 00:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_ios_push_tokens"
down_revision: str | None = "backfill_email_attachment_ids"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "ios_push_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("device_token", sa.String(length=255), nullable=False),
        sa.Column("user_identifier", sa.String(length=255), nullable=False),
        sa.Column(
            "environment",
            sa.String(length=20),
            server_default="production",
            nullable=False,
        ),
        sa.Column("bundle_id", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("device_token"),
    )
    op.create_index(
        op.f("ix_ios_push_tokens_device_token"),
        "ios_push_tokens",
        ["device_token"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ios_push_tokens_user_identifier"),
        "ios_push_tokens",
        ["user_identifier"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_ios_push_tokens_user_identifier"),
        table_name="ios_push_tokens",
    )
    op.drop_index(
        op.f("ix_ios_push_tokens_device_token"),
        table_name="ios_push_tokens",
    )
    op.drop_table("ios_push_tokens")
