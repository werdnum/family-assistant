"""Add scripts table for stored reusable automation scripts.

Revision ID: add_scripts_table
Revises: add_token_type_parent
Create Date: 2026-04-10

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "add_scripts_table"
down_revision: str | None = "add_token_type_parent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scripts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("script_code", sa.Text(), nullable=False),
        sa.Column("parameters_schema", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_scripts_name"), "scripts", ["name"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_scripts_name"), table_name="scripts")
    op.drop_table("scripts")
