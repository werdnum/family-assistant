"""Add creator provenance to automation tables

Adds ``processing_profile_id`` and ``created_by_user_id`` to the
``event_listeners`` and ``schedule_automations`` tables so that scripts are
validated and executed under the processing profile (and on behalf of the user)
that created them, rather than the task worker's default profile.

Revision ID: add_automation_provenance
Revises: add_ios_push_tokens
Create Date: 2026-06-10 00:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_automation_provenance"
down_revision: str | None = "add_ios_push_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLES = ("event_listeners", "schedule_automations")


def upgrade() -> None:
    """Upgrade schema."""
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column("processing_profile_id", sa.String(length=255), nullable=True),
        )
        op.add_column(
            table,
            sa.Column("created_by_user_id", sa.String(length=255), nullable=True),
        )


def downgrade() -> None:
    """Downgrade schema."""
    for table in _TABLES:
        op.drop_column(table, "created_by_user_id")
        op.drop_column(table, "processing_profile_id")
