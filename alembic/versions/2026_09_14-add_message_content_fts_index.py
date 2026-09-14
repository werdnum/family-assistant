"""Index message content for conversation-list search.

Revision ID: add_message_content_fts_index
Revises: add_delegation_reconciliation
Create Date: 2026-09-14

A GIN index on ``to_tsvector('simple', content)`` backs the per-word predicates
of ``GET /api/v1/chat/conversations?q=``. The expression must stay identical to
``MESSAGE_CONTENT_TSVECTOR`` in ``storage/message_history.py``, or the planner
cannot use it. PostgreSQL only: SQLite searches by substring and gets no index.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "add_message_content_fts_index"
down_revision: str | None = "add_delegation_reconciliation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "ix_message_history_content_fts_gin"


def upgrade() -> None:
    """Create the full-text index on PostgreSQL."""
    if op.get_bind().dialect.name != "postgresql":
        return
    op.create_index(
        INDEX_NAME,
        "message_history",
        [sa.text("to_tsvector('simple'::regconfig, content)")],
        postgresql_using="gin",
        if_not_exists=True,
    )


def downgrade() -> None:
    """Drop the full-text index on PostgreSQL."""
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_index(INDEX_NAME, table_name="message_history", if_exists=True)
