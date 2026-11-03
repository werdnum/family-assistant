"""Store automations' interface_type as a plain string.

The ``interfacetype`` enum named only telegram, web and email, so an
automation created from any other interface (an MCP conversation, voice) was
refused by the database. Every other table stores interface_type as
VARCHAR(50); these two now do too.

Revision ID: interface_type_to_string
Revises: add_thread_start_marker
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "interface_type_to_string"
down_revision: str | None = "add_thread_start_marker"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("event_listeners", "schedule_automations")
_ENUM_VALUES = ("telegram", "web", "email")


def _retype(
    table: str, *, old: sa.types.TypeEngine, new: sa.types.TypeEngine, using: str
) -> None:
    # PostgreSQL cannot cast the enum-typed server default along with the
    # column, so the default comes off for the change and goes back after.
    with op.batch_alter_table(table) as batch:
        batch.alter_column("interface_type", existing_type=old, server_default=None)
        batch.alter_column(
            "interface_type",
            existing_type=old,
            type_=new,
            existing_nullable=False,
            postgresql_using=using,
        )
        batch.alter_column(
            "interface_type", existing_type=new, server_default="telegram"
        )


def upgrade() -> None:
    enum_type = sa.Enum(*_ENUM_VALUES, name="interfacetype")
    for table in _TABLES:
        _retype(
            table,
            old=enum_type,
            new=sa.String(length=50),
            using="interface_type::text",
        )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        postgresql.ENUM(name="interfacetype").drop(bind, checkfirst=True)


def downgrade() -> None:
    # Fails if any row holds an interface outside the old enum; those rows
    # have no representation in the old schema.
    bind = op.get_bind()
    enum_type = sa.Enum(*_ENUM_VALUES, name="interfacetype")
    if bind.dialect.name == "postgresql":
        enum_type.create(bind, checkfirst=True)
    for table in _TABLES:
        _retype(
            table,
            old=sa.String(length=50),
            new=enum_type,
            using="interface_type::interfacetype",
        )
