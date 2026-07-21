"""Convert a2a_tasks JSON columns to JSONB on PostgreSQL.

Revision ID: a2a_tasks_json_to_jsonb
Revises: add_tasks_original_task_id_idx
Create Date: 2026-07-21

The ``a2a_tasks`` model declares ``artifacts_json``, ``history_json`` and
``metadata_json`` as ``JSON().with_variant(JSONB, "postgresql")``, but the
table-creation migration made them plain ``JSON``. On PostgreSQL that left the
columns as ``json`` rather than ``jsonb``, drifting from the model. This
migration aligns them. It is a no-op on SQLite, where ``JSON`` and the JSONB
variant are stored identically.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a2a_tasks_json_to_jsonb"
down_revision: str | None = "add_tasks_original_task_id_idx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ("artifacts_json", "history_json", "metadata_json")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for column in _COLUMNS:
        op.alter_column(
            "a2a_tasks",
            column,
            type_=postgresql.JSONB(astext_type=sa.Text()),
            existing_type=postgresql.JSON(astext_type=sa.Text()),
            existing_nullable=True,
            postgresql_using=f"{column}::jsonb",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for column in _COLUMNS:
        op.alter_column(
            "a2a_tasks",
            column,
            type_=postgresql.JSON(astext_type=sa.Text()),
            existing_type=postgresql.JSONB(astext_type=sa.Text()),
            existing_nullable=True,
            postgresql_using=f"{column}::json",
        )
