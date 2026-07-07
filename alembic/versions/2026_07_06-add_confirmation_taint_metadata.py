"""Add taint metadata to confirmation requests

Revision ID: confirmation_taint_metadata
Revises: taint_audit_events
Create Date: 2026-07-06 00:30:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "confirmation_taint_metadata"
down_revision: str | None = "taint_audit_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "confirmation_requests",
        sa.Column(
            "taint_state_json",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "confirmation_requests",
        sa.Column("sink_class", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "confirmation_requests",
        sa.Column("static_policy_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "confirmation_requests",
        sa.Column("taint_policy_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "confirmation_requests",
        sa.Column("approval_policy_fingerprint", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("confirmation_requests", "approval_policy_fingerprint")
    op.drop_column("confirmation_requests", "taint_policy_reason")
    op.drop_column("confirmation_requests", "static_policy_reason")
    op.drop_column("confirmation_requests", "sink_class")
    op.drop_column("confirmation_requests", "taint_state_json")
