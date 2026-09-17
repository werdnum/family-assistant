"""Add the memory review watermarks and the contribution enablement boundary.

Revision ID: add_memory_review_state
Revises: add_memory_change_log
Create Date: 2026-09-17

The two pieces of stored state the review sweep is scheduled from: how far each
conversation has been reviewed, and when contribution was last turned on for
each profile.

See ``docs/design/conversation-memory.md``.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "add_memory_review_state"
down_revision: str | None = "add_memory_change_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_review_watermarks",
        sa.Column("interface_type", sa.String(length=50), nullable=False),
        sa.Column("conversation_id", sa.String(length=255), nullable=False),
        sa.Column("last_reviewed_internal_id", sa.Integer(), nullable=False),
        sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "interface_type", "conversation_id", name="pk_memory_review_watermarks"
        ),
    )
    op.create_table(
        "memory_contribution_state",
        sa.Column("profile_id", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("profile_id"),
    )
    op.create_index(
        "ix_memory_contribution_state_enabled",
        "memory_contribution_state",
        ["enabled"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_memory_contribution_state_enabled", table_name="memory_contribution_state"
    )
    op.drop_table("memory_contribution_state")
    op.drop_table("memory_review_watermarks")
