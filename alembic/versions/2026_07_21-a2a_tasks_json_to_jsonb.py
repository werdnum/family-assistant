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

Each column conversion is guarded on its current type: databases bootstrapped via
``init_db``'s ``metadata.create_all()`` path already have ``jsonb`` (the model
declares the variant), so this only rewrites the ``json`` columns of
migration-built databases and avoids a needless table rewrite otherwise.
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


def _column_type_names(bind: sa.Connection) -> dict[str, str]:
    inspector = sa.inspect(bind)
    return {
        col["name"]: type(col["type"]).__name__.lower()
        for col in inspector.get_columns("a2a_tasks")
    }


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    types = _column_type_names(bind)
    for column in _COLUMNS:
        if types.get(column) == "jsonb":
            continue
        op.alter_column(
            "a2a_tasks",
            column,
            type_=postgresql.JSONB(astext_type=sa.Text()),
            existing_type=postgresql.JSON(astext_type=sa.Text()),
            existing_nullable=True,
            postgresql_using=f"{column}::jsonb",
        )


def downgrade() -> None:
    """No-op by design.

    The JSONB column variant is model-declared, so a database bootstrapped via
    ``metadata.create_all()`` already stores these columns as ``jsonb``
    independently of this migration — its upgrade skipped the conversion.
    Because the migration cannot tell whether it performed the conversion,
    converting back to ``json`` here would leave such databases drifted from the
    model for no benefit. The columns are dropped anyway when the ``a2a_tasks``
    table itself is dropped further down the downgrade chain.
    """
