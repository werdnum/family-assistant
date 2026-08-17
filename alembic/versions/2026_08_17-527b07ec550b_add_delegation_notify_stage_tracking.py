"""Add delegation notify stage tracking

Revision ID: 527b07ec550b
Revises: conversation_shares
Create Date: 2026-08-17 11:10:16.343215+10:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "527b07ec550b"
down_revision: str | None = "conversation_shares"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Track how far a terminal delegation run got towards reaching the requester."""
    op.add_column(
        "delegation_runs",
        sa.Column(
            "notify_stage",
            sa.String(length=20),
            server_default="initial",
            nullable=False,
        ),
    )
    op.add_column(
        "delegation_runs",
        sa.Column("notify_attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "delegation_runs",
        sa.Column("notify_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "delegation_runs",
        sa.Column("notify_first_failed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Drop the delivery-stage tracking columns."""
    op.drop_column("delegation_runs", "notify_first_failed_at")
    op.drop_column("delegation_runs", "notify_error")
    op.drop_column("delegation_runs", "notify_attempts")
    op.drop_column("delegation_runs", "notify_stage")
