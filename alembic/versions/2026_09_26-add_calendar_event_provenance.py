"""Track assistant-written calendar events by version and inherited taint.

Revision ID: add_calendar_event_provenance
Revises: add_schedule_recurrence_anchor
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "add_calendar_event_provenance"
down_revision: str | None = "add_schedule_recurrence_anchor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "calendar_event_provenance",
        sa.Column("marker", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.String(255), nullable=True),
        sa.Column("source_key", sa.String(1024), nullable=False),
        sa.Column("event_uid", sa.String(1024), nullable=False),
        sa.Column("event_version", sa.String(255), nullable=False),
        sa.Column("taint_metadata", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("calendar_event_provenance")
