"""Add the memory change log.

Revision ID: add_memory_change_log
Revises: add_memory_store
Create Date: 2026-09-17

One row per applied memory edit, plus rows for reviews that ended without any
(skipped, abandoned), which is what the recent-changes view reads.

See ``docs/design/conversation-memory.md``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "add_memory_change_log"
down_revision: str | None = "add_memory_store"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_change_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("actor_kind", sa.String(length=32), nullable=False),
        sa.Column("actor_identity", sa.String(length=255), nullable=True),
        sa.Column("interface_type", sa.String(length=50), nullable=True),
        sa.Column("conversation_id", sa.String(length=255), nullable=True),
        sa.Column("op", sa.String(length=16), nullable=True),
        sa.Column("note_title", sa.String(length=255), nullable=True),
        sa.Column("destination_note_title", sa.String(length=255), nullable=True),
        sa.Column("before_text", sa.Text(), nullable=True),
        sa.Column("after_text", sa.Text(), nullable=True),
        sa.Column(
            "evidence_message_ids",
            sa.JSON().with_variant(JSONB, "postgresql"),
            nullable=True,
        ),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_change_log_created_at", "memory_change_log", ["created_at"]
    )
    op.create_index("ix_memory_change_log_batch_id", "memory_change_log", ["batch_id"])


def downgrade() -> None:
    op.drop_index("ix_memory_change_log_batch_id", table_name="memory_change_log")
    op.drop_index("ix_memory_change_log_created_at", table_name="memory_change_log")
    op.drop_table("memory_change_log")
