"""Add the single-row household memory store.

Revision ID: add_memory_store
Revises: add_message_content_fts_index
Create Date: 2026-09-17

Holds the revision every conversation-memory write bumps and the identity of
the always-loaded core memory note. The row itself is created lazily by the
repository on the first memory write rather than seeded here, so a schema built
from the metadata (as the test fixtures build it) behaves the same way as a
migrated one.

See ``docs/design/conversation-memory.md``.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "add_memory_store"
down_revision: str | None = "add_message_content_fts_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_store",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("revision", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("core_note_id", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_memory_store_singleton"),
        sa.ForeignKeyConstraint(["core_note_id"], ["notes.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("memory_store")
