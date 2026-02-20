"""Add a2a_tasks table for A2A protocol task persistence.

Revision ID: add_a2a_tasks
Revises: add_is_skill_notes
Create Date: 2026-02-20

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_a2a_tasks"
down_revision: str | None = "add_is_skill_notes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "a2a_tasks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.String(length=100), nullable=False),
        sa.Column("context_id", sa.String(length=100), nullable=True),
        sa.Column("profile_id", sa.String(length=100), nullable=False),
        sa.Column("conversation_id", sa.String(length=100), nullable=False),
        sa.Column(
            "status",
            sa.String(length=50),
            server_default="submitted",
            nullable=False,
        ),
        sa.Column("artifacts_json", sa.JSON(), nullable=True),
        sa.Column("history_json", sa.JSON(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_a2a_tasks_task_id"), "a2a_tasks", ["task_id"], unique=True)
    op.create_index(
        op.f("ix_a2a_tasks_context_id"), "a2a_tasks", ["context_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_a2a_tasks_context_id"), table_name="a2a_tasks")
    op.drop_index(op.f("ix_a2a_tasks_task_id"), table_name="a2a_tasks")
    op.drop_table("a2a_tasks")
