"""Mark the row that tells a user script review refused to run an automation.

Revision ID: add_review_rejection_notice
Revises: add_claude_agent_sessions
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "add_review_rejection_notice"
down_revision: str | None = "add_claude_agent_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("message_history") as batch:
        batch.add_column(
            sa.Column(
                "review_rejection",
                sa.JSON().with_variant(JSONB, "postgresql"),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column("review_rejection_key", sa.String(length=64), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("message_history") as batch:
        batch.drop_column("review_rejection_key")
        batch.drop_column("review_rejection")
