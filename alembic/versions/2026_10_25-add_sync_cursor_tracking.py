"""Track a commit-ordered sync cursor on the rows clients mirror

Revision ID: sync_cursor_tracking
Revises: delegation_originating_request
Create Date: 2026-10-25

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "sync_cursor_tracking"
down_revision: str | None = "delegation_originating_request"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SYNCED_TABLES = (
    "message_history",
    "conversation_threads",
    "conversation_read_markers",
    "confirmation_requests",
    "delegation_runs",
    "turn_feedback",
)


def upgrade() -> None:
    counter = op.create_table(
        "sync_cursor_counter",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("value", sa.BigInteger(), nullable=False),
    )
    op.bulk_insert(counter, [{"id": 1, "value": 0}])
    op.create_table(
        "sync_tombstones",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("sync_cursor", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.String(255), nullable=False),
        sa.Column("conversation_id", sa.String(255), nullable=True),
        sa.Column("owner_user_id", sa.String(255), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_sync_tombstones_sync_cursor", "sync_tombstones", ["sync_cursor"]
    )
    for table in _SYNCED_TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "sync_cursor", sa.BigInteger(), nullable=False, server_default="0"
                )
            )
            batch_op.create_index(f"ix_{table}_sync_cursor", ["sync_cursor"])


def downgrade() -> None:
    for table in _SYNCED_TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_index(f"ix_{table}_sync_cursor")
            batch_op.drop_column("sync_cursor")
    op.drop_index("ix_sync_tombstones_sync_cursor", table_name="sync_tombstones")
    op.drop_table("sync_tombstones")
    op.drop_table("sync_cursor_counter")
