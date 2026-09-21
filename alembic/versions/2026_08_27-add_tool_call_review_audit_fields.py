"""Add tool-call review fields to taint audit events.

Revision ID: tool_call_review_audit
Revises: 527b07ec550b
Create Date: 2026-08-27 00:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "tool_call_review_audit"
down_revision: str | None = "527b07ec550b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Persist reviewer verdicts, status, latency, and delegating context."""
    op.add_column(
        "taint_audit_events",
        sa.Column("review_verdict", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "taint_audit_events",
        sa.Column("review_status", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "taint_audit_events",
        sa.Column("review_latency_ms", sa.Float(), nullable=True),
    )
    op.add_column(
        "taint_audit_events",
        sa.Column(
            "review_context_json",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_taint_audit_events_review_verdict",
        "taint_audit_events",
        ["review_verdict"],
    )
    op.create_index(
        "ix_taint_audit_events_review_status",
        "taint_audit_events",
        ["review_status"],
    )


def downgrade() -> None:
    """Drop tool-call review audit fields."""
    op.drop_index(
        "ix_taint_audit_events_review_status",
        table_name="taint_audit_events",
    )
    op.drop_index(
        "ix_taint_audit_events_review_verdict",
        table_name="taint_audit_events",
    )
    op.drop_column("taint_audit_events", "review_context_json")
    op.drop_column("taint_audit_events", "review_latency_ms")
    op.drop_column("taint_audit_events", "review_status")
    op.drop_column("taint_audit_events", "review_verdict")
