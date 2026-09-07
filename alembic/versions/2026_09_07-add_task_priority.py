"""Add the priority lane column to the task queue.

Revision ID: add_task_priority
Revises: d7065490c04e
Create Date: 2026-09-07

The column arrives with the background value as its server default, which is
the safe answer for a row whose producer did not choose. Rows already in the
queue at upgrade time did have a producer, though, and demoting an in-flight
reminder or a delegation poll behind a backfill walk is exactly the starvation
the lanes exist to end -- so the pending population is classified by task type
before the queue starts serving it.

Type is the right key for these rows even though the lane is a property of the
task: the one type that serves both lanes, ``embed_and_store_batch``, has only
user-initiated producers today, so every row of it that can exist at upgrade
time is interactive. See ``docs/design/task-queue-priority-lanes.md``.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "add_task_priority"
down_revision: str | None = "d7065490c04e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept as literals rather than imported from family_assistant.storage.tasks: a
# migration describes the schema at one point in history, and must keep applying
# the same way after the enum moves or grows a member.
_BACKGROUND = 0
_INTERACTIVE = 1

_INTERACTIVE_TASK_TYPES = (
    "llm_callback",
    "confirmation_tool_execution",
    "delegated_profile_run",
    "delegation_poll",
    "script_execution",
    "email_intake_action",
    "process_uploaded_document",
    "reindex_document",
    "index_email",
    "embed_and_store_batch",
    "schedule_automation_advance",
)

# The background lane is the column's server default, so message-history
# indexing, note indexing, message logging and every cleanup and reaper type
# land there without a statement of their own.

_tasks_table = sa.table(
    "tasks",
    sa.column("task_type", sa.String),
    sa.column("priority", sa.Integer),
)


def upgrade() -> None:
    """Add ``tasks.priority`` and classify the rows already in the queue."""
    op.add_column(
        "tasks",
        sa.Column(
            "priority",
            sa.Integer(),
            nullable=False,
            server_default=str(_BACKGROUND),
        ),
    )
    op.execute(
        _tasks_table
        .update()
        .where(_tasks_table.c.task_type.in_(_INTERACTIVE_TASK_TYPES))
        .values(priority=_INTERACTIVE)
    )


def downgrade() -> None:
    """Drop the priority lane column."""
    op.drop_column("tasks", "priority")
