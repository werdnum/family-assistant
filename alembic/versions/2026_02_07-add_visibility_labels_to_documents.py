"""Add visibility_labels column to documents table

Revision ID: add_visibility_labels_docs
Revises: add_visibility_labels_notes
Create Date: 2026-02-07

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_visibility_labels_docs"
down_revision: str | None = "add_visibility_labels_notes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add visibility_labels column to documents table and backfill from notes."""
    op.add_column(
        "documents",
        sa.Column(
            "visibility_labels",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )
    # Backfill note documents from the notes table
    op.execute(
        sa.text(
            "UPDATE documents SET visibility_labels = "
            "(SELECT notes.visibility_labels FROM notes "
            "WHERE notes.title = documents.source_id) "
            "WHERE source_type = 'note' "
            "AND EXISTS (SELECT 1 FROM notes WHERE notes.title = documents.source_id)"
        )
    )


def downgrade() -> None:
    """Remove visibility_labels column from documents table."""
    op.drop_column("documents", "visibility_labels")
