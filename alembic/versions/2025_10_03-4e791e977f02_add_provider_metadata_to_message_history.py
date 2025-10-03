"""add_provider_metadata_to_message_history

Revision ID: 4e791e977f02
Revises: f2e47092d2be
Create Date: 2025-10-03 12:29:19.601259+10:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4e791e977f02"
down_revision: str | None = "f2e47092d2be"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add provider_metadata column to message_history table."""
    op.add_column(
        "message_history",
        sa.Column(
            "provider_metadata",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Remove provider_metadata column from message_history table."""
    op.drop_column("message_history", "provider_metadata")
