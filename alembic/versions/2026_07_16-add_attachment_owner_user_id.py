"""Add owner_user_id to attachment_metadata

Revision ID: attachment_owner_user_id
Revises: google_connections
Create Date: 2026-07-16 00:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "attachment_owner_user_id"
down_revision: str | None = "google_connections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "attachment_metadata",
        sa.Column("owner_user_id", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("attachment_metadata", "owner_user_id")
