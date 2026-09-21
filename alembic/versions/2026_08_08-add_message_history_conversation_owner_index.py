"""Add the partial (conversation_id, user_id) index on message_history.

Revision ID: mh_conversation_owner_idx
Revises: restamp_automation_creation
Create Date: 2026-08-08

The conversation list restricts results to conversations the caller solely owns,
expressed as a correlated ``NOT EXISTS`` over user messages carrying a foreign
``user_id``. PostgreSQL had no index matching that predicate's shape and
answered it from ``ix_message_history_user_id``, reading every indexed row and
discarding the ones whose role was not ``user``.

Partial on ``role = 'user' AND user_id IS NOT NULL`` -- exactly the rows the
predicate can match -- so it stays small on a table that takes a write per
message.

Guarded on the index not already existing: databases bootstrapped via
``init_db``'s ``metadata.create_all()`` already have it from the model
declaration and were stamped at a later head, so an unconditional
``CREATE INDEX`` would abort their upgrade.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "mh_conversation_owner_idx"
down_revision: str | None = "restamp_automation_creation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "ix_message_history_conversation_owner"
_WHERE = "role = 'user' AND user_id IS NOT NULL"


def _index_exists(bind: sa.Connection) -> bool:
    inspector = sa.inspect(bind)
    return any(
        ix["name"] == _INDEX_NAME for ix in inspector.get_indexes("message_history")
    )


def upgrade() -> None:
    bind = op.get_bind()
    if _index_exists(bind):
        return
    dialect = bind.dialect.name
    kwargs = (
        {"postgresql_where": sa.text(_WHERE)}
        if dialect == "postgresql"
        else {"sqlite_where": sa.text(_WHERE)}
        if dialect == "sqlite"
        else {}
    )
    op.create_index(
        _INDEX_NAME,
        "message_history",
        ["conversation_id", "user_id"],
        unique=False,
        **kwargs,
    )


def downgrade() -> None:
    """Drop the index, unlike the no-op downgrades of other index migrations.

    A downgrade further along this chain drops ``message_history.user_id``, and
    SQLite implements that by rebuilding the table -- which fails outright while
    an index still references the dropped column. So this one has to come off,
    whether it arrived via this migration or via ``metadata.create_all()``.
    """
    bind = op.get_bind()
    if _index_exists(bind):
        op.drop_index(_INDEX_NAME, table_name="message_history")
