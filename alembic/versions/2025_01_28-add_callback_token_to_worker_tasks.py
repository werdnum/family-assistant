"""Add callback_token column to worker_tasks table.

This column stores a cryptographically random token that workers must
include in their completion webhooks for verification.

Revision ID: add_callback_token
Revises: add_worker_tasks
Create Date: 2025-01-28

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_callback_token"
down_revision: str = "add_worker_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add callback_token column to worker_tasks table."""
    op.add_column(
        "worker_tasks",
        sa.Column("callback_token", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    """Remove callback_token column from worker_tasks table."""
    op.drop_column("worker_tasks", "callback_token")
