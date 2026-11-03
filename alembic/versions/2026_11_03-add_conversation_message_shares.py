"""Add read-only share links to single messages.

Revision ID: add_conversation_message_shares
Revises: add_thread_start_marker
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "add_conversation_message_shares"
down_revision: str | None = "add_thread_start_marker"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversation_message_shares",
        sa.Column("message_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("conversation_id", sa.String(length=255), nullable=False),
        sa.Column("owner_user_id", sa.String(length=255), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("message_id"),
    )
    op.create_index(
        "ix_conversation_message_shares_conversation_id",
        "conversation_message_shares",
        ["conversation_id"],
    )
    op.create_index(
        "ix_conversation_message_shares_owner_user_id",
        "conversation_message_shares",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_conversation_message_shares_token_hash",
        "conversation_message_shares",
        ["token_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversation_message_shares_token_hash",
        table_name="conversation_message_shares",
    )
    op.drop_index(
        "ix_conversation_message_shares_owner_user_id",
        table_name="conversation_message_shares",
    )
    op.drop_index(
        "ix_conversation_message_shares_conversation_id",
        table_name="conversation_message_shares",
    )
    op.drop_table("conversation_message_shares")
