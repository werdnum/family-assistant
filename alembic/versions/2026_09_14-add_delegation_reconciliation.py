"""Add remote-observation and reconciliation fields to delegation runs.

Revision ID: add_delegation_reconciliation
Revises: add_task_priority
Create Date: 2026-09-14

Splits a delegation run's own disposition from what the provider was last seen
to say, and adds the bookkeeping the reconciliation sweep needs. Every column
is nullable or carries a server default, so runs already in flight at upgrade
time keep their current behaviour: with no ``local_failure_kind`` they are not
eligible for reconciliation, which is the right answer for a run whose failure
predates the distinction.

See ``docs/design/delegation-remote-reconciliation.md``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "add_delegation_reconciliation"
down_revision: str | None = "add_task_priority"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the local-disposition, remote-observation and reconciliation columns."""
    op.add_column(
        "delegation_runs",
        sa.Column("local_failure_kind", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "delegation_runs",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "delegation_runs",
        sa.Column("cancel_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "delegation_runs",
        sa.Column("remote_status", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "delegation_runs",
        sa.Column("remote_observed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "delegation_runs",
        sa.Column(
            "remote_observation_json",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "delegation_runs",
        sa.Column(
            "reconcile_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "delegation_runs",
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "delegation_runs",
        sa.Column("late_recovered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_delegation_runs_reconciled_completed",
        "delegation_runs",
        ["reconciled_at", "completed_at"],
    )


def downgrade() -> None:
    """Drop the reconciliation columns and their index."""
    op.drop_index(
        "ix_delegation_runs_reconciled_completed", table_name="delegation_runs"
    )
    for column in (
        "late_recovered_at",
        "reconciled_at",
        "reconcile_attempts",
        "remote_observation_json",
        "remote_observed_at",
        "remote_status",
        "cancel_confirmed_at",
        "cancel_requested_at",
        "local_failure_kind",
    ):
        op.drop_column("delegation_runs", column)
