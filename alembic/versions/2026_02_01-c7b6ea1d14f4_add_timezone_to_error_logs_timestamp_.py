"""Add timezone to error_logs timestamp column

Revision ID: c7b6ea1d14f4
Revises: add_callback_token
Create Date: 2026-02-01 18:56:58.526108+11:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7b6ea1d14f4"
down_revision: str | None = "add_callback_token"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    dialect_name = op.get_context().dialect.name

    if dialect_name == "postgresql":
        # PostgreSQL: alter column to add timezone support
        op.alter_column(
            "error_logs",
            "timestamp",
            existing_type=postgresql.TIMESTAMP(),
            type_=sa.DateTime(timezone=True),
            existing_nullable=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    dialect_name = op.get_context().dialect.name

    if dialect_name == "postgresql":
        op.alter_column(
            "error_logs",
            "timestamp",
            existing_type=sa.DateTime(timezone=True),
            type_=postgresql.TIMESTAMP(),
            existing_nullable=False,
        )
