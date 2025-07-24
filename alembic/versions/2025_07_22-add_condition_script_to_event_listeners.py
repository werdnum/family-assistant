"""Add condition_script to event_listeners

Revision ID: add_condition_script
Revises: 8c4cf2ceddd1
Create Date: 2025-07-22 12:30:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_condition_script"
down_revision: str | None = "8c4cf2ceddd1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add condition_script column to event_listeners table."""
    op.add_column(
        "event_listeners",
        sa.Column("condition_script", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Remove condition_script column from event_listeners table."""
    op.drop_column("event_listeners", "condition_script")
