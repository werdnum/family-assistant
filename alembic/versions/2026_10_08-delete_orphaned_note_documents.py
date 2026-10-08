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
    sa.column("id", sa.Integer),
    sa.column("source_type", sa.String),
    sa.column("source_id", sa.Text),
)
document_embeddings = sa.table(
    "document_embeddings", sa.column("document_id", sa.Integer)
)


def upgrade() -> None:
    """Delete note index records whose note no longer exists, with their embeddings."""
    orphaned_ids = sa.select(documents.c.id).where(
        documents.c.source_type == "note",
        ~documents.c.source_id.in_(sa.select(notes.c.title)),
    )
    # SQLite deployments have no embeddings table; it needs pgvector.
    if sa.inspect(op.get_bind()).has_table("document_embeddings"):
        op.execute(
            sa.delete(document_embeddings).where(
                document_embeddings.c.document_id.in_(orphaned_ids)
            )
        )
    op.execute(sa.delete(documents).where(documents.c.id.in_(orphaned_ids)))


def downgrade() -> None:
    """Deleted index records are not restored; re-indexing recreates live ones."""
