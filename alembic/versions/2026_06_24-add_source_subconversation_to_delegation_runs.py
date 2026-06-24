"""Add source subconversation to delegation runs

Revision ID: source_subconv_delegation
Revises: e909ea33321b
Create Date: 2026-06-24 04:20:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "source_subconv_delegation"
down_revision: str | None = "e909ea33321b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "delegation_runs",
        sa.Column("source_subconversation_id", sa.String(length=36), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("delegation_runs", "source_subconversation_id")
