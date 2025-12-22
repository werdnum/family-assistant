"""Add attachment_ids column to notes table

Revision ID: add_attachment_ids_notes
Revises: 2025_11_07-add_subconversation_id_to_message_history
Create Date: 2025-12-22

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_attachment_ids_notes"
down_revision: str | None = "f2a1b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add attachment_ids column to notes table."""
    # Add attachment_ids column as TEXT (JSON array of attachment UUIDs)
    # Default to empty array '[]'
    op.add_column(
        "notes",
        sa.Column(
            "attachment_ids",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    """Remove attachment_ids column from notes table."""
    op.drop_column("notes", "attachment_ids")
