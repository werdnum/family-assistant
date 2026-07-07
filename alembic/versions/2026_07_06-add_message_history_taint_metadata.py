"""Add taint metadata to message history

Revision ID: message_history_taint
Revises: internal_message_history
Create Date: 2026-07-06 00:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "message_history_taint"
down_revision: str | None = "internal_message_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "message_history",
        sa.Column(
            "taint_metadata_json",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "message_history",
        sa.Column("taint_metadata_version", sa.String(length=64), nullable=True),
    )
    message_history = sa.table(
        "message_history",
        sa.column("role", sa.String()),
        sa.column("taint_metadata_json", sa.JSON()),
        sa.column("taint_metadata_version", sa.String(length=64)),
    )
    op.execute(
        message_history
        .update()
        .where(message_history.c.role.in_(("user", "assistant", "tool")))
        .where(message_history.c.taint_metadata_json.is_(None))
        .values(
            taint_metadata_json={
                "version": "runtime_v1",
                "max_tier": "unknown_external",
                "history_high_taint_present": True,
                "fresh_high_taint_seen_at_sequence": None,
                "sources": [
                    {
                        "source_type": "manual",
                        "source_id": None,
                        "tier": "unknown_external",
                        "labels": ["legacy_missing_taint_metadata"],
                        "reason": (
                            "Message history row predates runtime taint metadata; "
                            "backfilled as unknown_external."
                        ),
                    }
                ],
            },
            taint_metadata_version="runtime_v1",
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("message_history", "taint_metadata_version")
    op.drop_column("message_history", "taint_metadata_json")
