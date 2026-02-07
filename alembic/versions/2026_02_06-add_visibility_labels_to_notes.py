"""Add visibility_labels column to notes table

Revision ID: add_visibility_labels_notes
Revises: c7b6ea1d14f4
Create Date: 2026-02-06

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_visibility_labels_notes"
down_revision: str | None = "c7b6ea1d14f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add visibility_labels column to notes table and backfill with ["default"]."""
    op.add_column(
        "notes",
        sa.Column(
            "visibility_labels",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )
    # Backfill existing notes with ["default"] so they require the "default" grant
    op.execute(
        sa.text("UPDATE notes SET visibility_labels = :labels").bindparams(
            labels='["default"]'
        )
    )


def downgrade() -> None:
    """Remove visibility_labels column from notes table."""
    op.drop_column("notes", "visibility_labels")
