"""Mark the Home row that records a thread the user started.

Revision ID: add_thread_start_marker
Revises: sync_cursor_tracking
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "add_thread_start_marker"
down_revision: str | None = "sync_cursor_tracking"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("message_history") as batch:
        batch.add_column(
            sa.Column(
                "started_thread_conversation_id", sa.String(length=255), nullable=True
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("message_history") as batch:
        batch.drop_column("started_thread_conversation_id")
