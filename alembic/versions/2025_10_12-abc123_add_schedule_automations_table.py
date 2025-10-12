"""Add schedule_automations table for unified automations system

Revision ID: abc123
Revises: 6d7dd88fefad
Create Date: 2025-10-12 09:50:00.000000+11:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "abc123"
down_revision: str | None = "6d7dd88fefad"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Create schedule_automations table (reuses existing enum types from event_listeners)
    op.create_table(
        "schedule_automations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("conversation_id", sa.String(length=255), nullable=False),
        sa.Column(
            "interface_type",
            postgresql.ENUM(
                "telegram", "web", "email", name="interfacetype", create_type=False
            ),
            nullable=False,
            server_default="telegram",
        ),
        sa.Column("recurrence_rule", sa.Text(), nullable=False),
        sa.Column("next_scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "action_type",
            postgresql.ENUM(
                "wake_llm", "script", name="eventactiontype", create_type=False
            ),
            nullable=False,
            server_default="wake_llm",
        ),
        sa.Column(
            "action_config",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_execution_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_count", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "name", "conversation_id", name="uq_sched_name_conversation"
        ),
    )
    op.create_index(
        op.f("ix_schedule_automations_conversation_id"),
        "schedule_automations",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        "idx_sched_conversation",
        "schedule_automations",
        ["conversation_id", "enabled"],
        unique=False,
    )
    op.create_index(
        "idx_sched_next",
        "schedule_automations",
        ["next_scheduled_at", "enabled"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("idx_sched_next", table_name="schedule_automations")
    op.drop_index("idx_sched_conversation", table_name="schedule_automations")
    op.drop_index(
        op.f("ix_schedule_automations_conversation_id"),
        table_name="schedule_automations",
    )
    op.drop_table("schedule_automations")
