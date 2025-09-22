"""Add attachment_metadata table for unified attachment tracking

Revision ID: 9ead44a79229
Revises: db7825a36aae
Create Date: 2025-09-22 13:41:04.819795+10:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9ead44a79229"
down_revision: str | None = "db7825a36aae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Create attachment_metadata table for unified attachment tracking
    op.create_table(
        "attachment_metadata",
        sa.Column("attachment_id", sa.String(36), primary_key=True),  # UUID
        sa.Column(
            "source_type", sa.String(20), nullable=False
        ),  # "user", "tool", "script"
        sa.Column(
            "source_id", sa.String(255), nullable=False
        ),  # user_id, tool_name, script_id
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("content_url", sa.Text(), nullable=True),  # URL for retrieval
        sa.Column("storage_path", sa.Text(), nullable=True),  # File system path
        sa.Column("conversation_id", sa.String(255), nullable=True),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accessed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "metadata",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )

    # Create indexes for common queries
    op.create_index(
        "idx_attachment_conversation", "attachment_metadata", ["conversation_id"]
    )
    op.create_index(
        "idx_attachment_source", "attachment_metadata", ["source_type", "source_id"]
    )
    op.create_index("idx_attachment_created", "attachment_metadata", ["created_at"])


def downgrade() -> None:
    """Downgrade schema."""
    # Drop indexes first
    op.drop_index("idx_attachment_created", "attachment_metadata")
    op.drop_index("idx_attachment_source", "attachment_metadata")
    op.drop_index("idx_attachment_conversation", "attachment_metadata")

    # Drop the table
    op.drop_table("attachment_metadata")
