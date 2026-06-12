"""Add decision_only flag to confirmation_requests

Revision ID: add_confirmation_decision_only
Revises: add_delegation_runs
Create Date: 2026-06-12 00:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_confirmation_decision_only"
down_revision: str | None = "add_delegation_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the durable decision_only flag.

    Records (durably) that an approved confirmation resumes a caller executing
    the tool inline (e.g. a delegated run waiting on the decision) rather than
    enqueueing a confirmation_tool_execution task, so the approval endpoint makes
    the right enqueue decision across restarts / processes.
    """
    op.add_column(
        "confirmation_requests",
        sa.Column(
            "decision_only",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    """Remove the decision_only flag."""
    op.drop_column("confirmation_requests", "decision_only")
