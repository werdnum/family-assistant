"""Add definition_record to executable definitions

Records the authoring stamp, executable-content hash, and creation disposition
beside each stored automation, event listener, and script, so a firing can
resolve what the creation gate decided instead of re-asking at a boundary that
cannot answer. See docs/design/executable-definition-taint.md.

Nullable with no backfill: a legacy definition has no record and keeps today's
fail-closed firing behaviour until it is touched or attested.

Revision ID: 631e7ea62ec4
Revises: tool_call_review_audit
Create Date: 2026-09-01 14:55:59.079895+10:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "631e7ea62ec4"
down_revision: str | None = "tool_call_review_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _record_column() -> sa.Column[object]:
    return sa.Column(
        "definition_record",
        sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
        nullable=True,
    )


def upgrade() -> None:
    """Add the nullable definition_record column to each definition table."""
    op.add_column("event_listeners", _record_column())
    op.add_column("schedule_automations", _record_column())
    op.add_column("scripts", sa.Column("definition_record", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the definition_record columns."""
    op.drop_column("scripts", "definition_record")
    op.drop_column("schedule_automations", "definition_record")
    op.drop_column("event_listeners", "definition_record")
