"""Add generated conversation-list summaries.

Revision ID: add_conversation_summaries
Revises: delete_orphaned_note_documents
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "add_conversation_summaries"
down_revision: str | None = "delete_orphaned_note_documents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversation_summaries",
        sa.Column("conversation_id", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("summarized_through_id", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("conversation_id"),
    )


def downgrade() -> None:
    op.drop_table("conversation_summaries")
