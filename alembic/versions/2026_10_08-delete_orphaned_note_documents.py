"""Delete search-index records left behind by deleted or renamed notes.

Revision ID: delete_orphaned_note_documents
Revises: add_taint_audit_previous_tier
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "delete_orphaned_note_documents"
down_revision: str | None = "add_taint_audit_previous_tier"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

notes = sa.table("notes", sa.column("title", sa.Text))
documents = sa.table(
    "documents",
    sa.column("source_type", sa.String),
    sa.column("source_id", sa.Text),
)


def upgrade() -> None:
    """Delete note index records whose note no longer exists.

    Their embeddings go with them through ``ON DELETE CASCADE`` on PostgreSQL;
    search joins through ``documents``, so any left on SQLite are unreachable.
    """
    op.execute(
        sa.delete(documents).where(
            documents.c.source_type == "note",
            ~documents.c.source_id.in_(sa.select(notes.c.title)),
        )
    )


def downgrade() -> None:
    """Deleted index records are not restored; re-indexing recreates live ones."""
