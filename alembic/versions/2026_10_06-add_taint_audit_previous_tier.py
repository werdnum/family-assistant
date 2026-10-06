"""Record the stored tier a note write replaced on its taint audit row.

Revision ID: add_taint_audit_previous_tier
Revises: merge_compaction_confirm_heads
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "add_taint_audit_previous_tier"
down_revision: str | None = "merge_compaction_confirm_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Keep the replaced tier beside the stored one on note tier drops."""
    op.add_column(
        "taint_audit_events",
        sa.Column("previous_tier", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_taint_audit_events_previous_tier",
        "taint_audit_events",
        ["previous_tier"],
    )


def downgrade() -> None:
    """Remove the replaced tier."""
    op.drop_index("ix_taint_audit_events_previous_tier", "taint_audit_events")
    op.drop_column("taint_audit_events", "previous_tier")
