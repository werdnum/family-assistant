"""Record a tool's own result tier on its taint audit row.

Revision ID: add_taint_audit_result_tier
Revises: add_delegation_origin_interface
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "add_taint_audit_result_tier"
down_revision: str | None = "add_delegation_origin_interface"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Keep the tool's own tier beside the turn's running maximum."""
    op.add_column(
        "taint_audit_events",
        sa.Column("result_tier", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_taint_audit_events_result_tier",
        "taint_audit_events",
        ["result_tier"],
    )


def downgrade() -> None:
    """Remove the per-tool result tier."""
    op.drop_index("ix_taint_audit_events_result_tier", "taint_audit_events")
    op.drop_column("taint_audit_events", "result_tier")
