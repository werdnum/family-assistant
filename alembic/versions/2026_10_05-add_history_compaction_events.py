"""Record history compaction events.

Revision ID: add_history_compaction_events
Revises: add_message_activated_tools
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "add_history_compaction_events"
down_revision: str | None = "add_message_activated_tools"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the table each window's compaction decisions are read back from."""
    op.create_table(
        "history_compaction_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("interface_type", sa.String(50), nullable=False),
        sa.Column("conversation_id", sa.String(255), nullable=False),
        sa.Column("processing_profile_id", sa.String(255), nullable=False),
        sa.Column("subconversation_id", sa.String(36), nullable=True),
        sa.Column("boundary_internal_id", sa.BigInteger(), nullable=False),
        sa.Column("active_turn_key", sa.String(64), nullable=True),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column(
            "decisions",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("changed", sa.Boolean(), nullable=False),
        sa.Column(
            "details",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_history_compaction_events_scope",
        "history_compaction_events",
        [
            "conversation_id",
            "interface_type",
            "processing_profile_id",
            "subconversation_id",
            "id",
        ],
    )


def downgrade() -> None:
    """Drop the compaction record."""
    op.drop_index(
        "ix_history_compaction_events_scope",
        table_name="history_compaction_events",
    )
    op.drop_table("history_compaction_events")
