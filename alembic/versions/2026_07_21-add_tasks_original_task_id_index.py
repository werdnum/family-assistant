"""Add missing index on tasks.original_task_id.

Revision ID: add_tasks_original_task_id_idx
Revises: delegation_active_subconv_unique
Create Date: 2026-07-21

The ``tasks`` table model declares ``original_task_id`` with ``index=True``, but
no migration ever created the corresponding index, so migration-built databases
drifted from the model. Recurrence lookups query by ``original_task_id``, so the
index is created here to match the model and back those lookups.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "add_tasks_original_task_id_idx"
down_revision: str | None = "delegation_active_subconv_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        op.f("ix_tasks_original_task_id"),
        "tasks",
        ["original_task_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_tasks_original_task_id"), table_name="tasks")
