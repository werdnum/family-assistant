"""Add worker_tasks table for AI worker sandbox.

Revision ID: add_worker_tasks
Revises: 2025_12_22-add_attachment_ids_to_notes
Create Date: 2025-01-25

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_worker_tasks"
down_revision: str = "add_attachment_ids_notes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create worker_tasks table."""
    op.create_table(
        "worker_tasks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.String(length=100), nullable=False),
        sa.Column("conversation_id", sa.String(length=255), nullable=False),
        sa.Column("interface_type", sa.String(length=50), nullable=False),
        sa.Column("user_name", sa.String(length=255), nullable=True),
        # Task configuration
        sa.Column(
            "model", sa.String(length=50), nullable=False, server_default="claude"
        ),
        sa.Column("task_description", sa.Text(), nullable=False),
        sa.Column(
            "context_files",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("timeout_minutes", sa.Integer(), nullable=False, server_default="30"),
        # Status tracking
        sa.Column(
            "status", sa.String(length=50), nullable=False, server_default="pending"
        ),
        sa.Column("job_name", sa.String(length=255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        # Results
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column(
            "output_files",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        # Metadata
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    # Create indexes
    op.create_index(
        op.f("ix_worker_tasks_task_id"), "worker_tasks", ["task_id"], unique=True
    )
    op.create_index(
        op.f("ix_worker_tasks_conversation_id"),
        "worker_tasks",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_worker_tasks_status"), "worker_tasks", ["status"], unique=False
    )


def downgrade() -> None:
    """Drop worker_tasks table."""
    op.drop_index(op.f("ix_worker_tasks_status"), table_name="worker_tasks")
    op.drop_index(op.f("ix_worker_tasks_conversation_id"), table_name="worker_tasks")
    op.drop_index(op.f("ix_worker_tasks_task_id"), table_name="worker_tasks")
    op.drop_table("worker_tasks")
