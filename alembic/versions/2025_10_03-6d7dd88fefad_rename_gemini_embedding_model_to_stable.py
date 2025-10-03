"""rename_gemini_embedding_model_to_stable

Migrate from experimental gemini-embedding-exp-03-07 to stable gemini-embedding-001.

Since the experimental and stable models produce compatible embeddings (same vector space),
we can simply rename the model identifier in the database without re-embedding content.

Per Google's blog post:
"If you are using the experimental gemini-embedding-exp-03-07, you won't need to re-embed
your contents but it will no longer be supported by the Gemini API on October 30, 2025."

Reference: https://developers.googleblog.com/en/gemini-embedding-available-gemini-api/

Revision ID: 6d7dd88fefad
Revises: 4e791e977f02
Create Date: 2025-10-03 19:37:58.374863+10:00

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6d7dd88fefad"
down_revision: str | None = "4e791e977f02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Rename experimental embedding model to stable model."""
    # Skip on SQLite - document_embeddings table requires PostgreSQL with pgvector
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    # Update all existing embeddings from experimental to stable model
    # Models are compatible - same vector space, no need to regenerate embeddings
    # Note: Safe to run on empty databases (no-op if no matching rows)
    op.execute("""
        UPDATE document_embeddings
        SET embedding_model = 'gemini/gemini-embedding-001'
        WHERE embedding_model = 'gemini/gemini-embedding-exp-03-07'
    """)


def downgrade() -> None:
    """Rollback to experimental model name."""
    # Skip on SQLite - document_embeddings table requires PostgreSQL with pgvector
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    # Revert model name change
    op.execute("""
        UPDATE document_embeddings
        SET embedding_model = 'gemini/gemini-embedding-exp-03-07'
        WHERE embedding_model = 'gemini/gemini-embedding-001'
    """)
