"""Add active read-only conversation shares.

Revision ID: conversation_shares
Revises: mh_conversation_owner_idx
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "conversation_shares"
down_revision: str | None = "mh_conversation_owner_idx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversation_shares",
        sa.Column("conversation_id", sa.String(length=255), nullable=False),
        sa.Column("owner_user_id", sa.String(length=255), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("conversation_id"),
    )
    op.create_index(
        "ix_conversation_shares_owner_user_id",
        "conversation_shares",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_conversation_shares_token_hash",
        "conversation_shares",
        ["token_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_shares_token_hash", table_name="conversation_shares")
    op.drop_index(
        "ix_conversation_shares_owner_user_id", table_name="conversation_shares"
    )
    op.drop_table("conversation_shares")
