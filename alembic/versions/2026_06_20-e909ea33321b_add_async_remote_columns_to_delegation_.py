"""add async remote columns to delegation_runs

Adds the columns the submit-then-poll A2A delegation path needs to track an
in-flight remote task: the remote A2A task/context ids and a poll-attempt
counter. All are null/zero for local delegations.

Revision ID: e909ea33321b
Revises: add_confirmation_decision_only
Create Date: 2026-06-20 17:34:56.207346+10:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e909ea33321b"
down_revision: str | None = "add_confirmation_decision_only"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "delegation_runs",
        sa.Column("remote_task_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "delegation_runs",
        sa.Column("remote_context_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "delegation_runs",
        sa.Column(
            "poll_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("delegation_runs", "poll_attempts")
    op.drop_column("delegation_runs", "remote_context_id")
    op.drop_column("delegation_runs", "remote_task_id")
