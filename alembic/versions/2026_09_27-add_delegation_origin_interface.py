"""Record the interface that initiated a delegated run.

Revision ID: add_delegation_origin_interface
Revises: add_calendar_event_provenance
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "add_delegation_origin_interface"
down_revision: str | None = "add_calendar_event_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Keep voice provenance when its history is stored as web Chat."""
    op.add_column(
        "delegation_runs",
        sa.Column("origin_interface_type", sa.String(length=50), nullable=True),
    )


def downgrade() -> None:
    """Remove the origin interface."""
    op.drop_column("delegation_runs", "origin_interface_type")
