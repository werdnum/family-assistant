"""Add missing index on tasks.original_task_id.

Revision ID: add_tasks_original_task_id_idx
Revises: drop_approval_fingerprint
Create Date: 2026-07-21

The ``tasks`` table model declares ``original_task_id`` with ``index=True``, but
no migration ever created the corresponding index, so migration-built databases
drifted from the model. Recurrence lookups query by ``original_task_id``, so the
index is created here to match the model and back those lookups.

The creation is guarded on the index not already existing: databases bootstrapped
via ``init_db``'s ``metadata.create_all()`` path already have the index (the model
declares ``index=True``) and were stamped at an earlier head, so an unconditional
``CREATE INDEX`` would abort their upgrade. Migration-built databases lack it and
get it here.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "add_tasks_original_task_id_idx"
down_revision: str | None = "drop_approval_fingerprint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "ix_tasks_original_task_id"


def _index_exists(bind: sa.Connection) -> bool:
    inspector = sa.inspect(bind)
    return any(ix["name"] == _INDEX_NAME for ix in inspector.get_indexes("tasks"))


def upgrade() -> None:
    bind = op.get_bind()
    if not _index_exists(bind):
        op.create_index(
            op.f(_INDEX_NAME),
            "tasks",
            ["original_task_id"],
            unique=False,
        )


def downgrade() -> None:
    """No-op by design.

    ``ix_tasks_original_task_id`` is model-declared (``tasks.original_task_id``
    is ``index=True``), so a database bootstrapped via ``metadata.create_all()``
    already has it independently of this migration — its upgrade skipped
    creation. Because the migration cannot tell whether it created the index,
    dropping it here would leave such databases drifted from the model for no
    benefit. The index is torn down anyway when the ``tasks`` table itself is
    dropped further down the downgrade chain.
    """
