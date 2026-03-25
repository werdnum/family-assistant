"""rename_embedding_model_to_native

Rename embedding model from LiteLLM format (gemini/gemini-embedding-001) to native
Google GenAI SDK format (gemini-embedding-001).

The embeddings themselves are unchanged - only the model identifier string is updated.

Revision ID: rename_to_native_embed
Revises: add_a2a_tasks
Create Date: 2026-03-25

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "rename_to_native_embed"
down_revision: str | None = "add_a2a_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Rename embedding model from LiteLLM to native format."""
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    op.execute("""
        UPDATE document_embeddings
        SET embedding_model = 'gemini-embedding-001'
        WHERE embedding_model = 'gemini/gemini-embedding-001'
    """)


def downgrade() -> None:
    """Revert to LiteLLM model name format."""
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    op.execute("""
        UPDATE document_embeddings
        SET embedding_model = 'gemini/gemini-embedding-001'
        WHERE embedding_model = 'gemini-embedding-001'
    """)
