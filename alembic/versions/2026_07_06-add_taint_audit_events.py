"""Add taint audit events table

Revision ID: taint_audit_events
Revises: note_provenance_metadata
Create Date: 2026-07-06 00:20:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "taint_audit_events"
down_revision: str | None = "note_provenance_metadata"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "taint_audit_events",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=255), nullable=False),
        sa.Column("turn_id", sa.String(length=255), nullable=True),
        sa.Column("processing_profile_id", sa.String(length=255), nullable=True),
        sa.Column("subconversation_id", sa.String(length=255), nullable=True),
        sa.Column("tool_name", sa.String(length=255), nullable=False),
        sa.Column("tool_call_id", sa.String(length=255), nullable=True),
        sa.Column("sink_class", sa.String(length=64), nullable=True),
        sa.Column("max_tier", sa.String(length=64), nullable=False),
        sa.Column(
            "sources_json",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("requested_outcome", sa.String(length=64), nullable=True),
        sa.Column("effective_outcome", sa.String(length=64), nullable=True),
        sa.Column("mode", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "arguments_summary_json",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("artifact_id", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_taint_audit_events_created_at",
        "taint_audit_events",
        ["created_at"],
    )
    op.create_index(
        "ix_taint_audit_events_event_type",
        "taint_audit_events",
        ["event_type"],
    )
    op.create_index(
        "ix_taint_audit_events_conversation_id",
        "taint_audit_events",
        ["conversation_id"],
    )
    op.create_index("ix_taint_audit_events_turn_id", "taint_audit_events", ["turn_id"])
    op.create_index(
        "ix_taint_audit_events_processing_profile_id",
        "taint_audit_events",
        ["processing_profile_id"],
    )
    op.create_index(
        "ix_taint_audit_events_subconversation_id",
        "taint_audit_events",
        ["subconversation_id"],
    )
    op.create_index(
        "ix_taint_audit_events_tool_name",
        "taint_audit_events",
        ["tool_name"],
    )
    op.create_index(
        "ix_taint_audit_events_tool_call_id",
        "taint_audit_events",
        ["tool_call_id"],
    )
    op.create_index(
        "ix_taint_audit_events_sink_class",
        "taint_audit_events",
        ["sink_class"],
    )
    op.create_index(
        "ix_taint_audit_events_max_tier",
        "taint_audit_events",
        ["max_tier"],
    )
    op.create_index(
        "ix_taint_audit_events_requested_outcome",
        "taint_audit_events",
        ["requested_outcome"],
    )
    op.create_index(
        "ix_taint_audit_events_effective_outcome",
        "taint_audit_events",
        ["effective_outcome"],
    )
    op.create_index("ix_taint_audit_events_mode", "taint_audit_events", ["mode"])
    op.create_index(
        "ix_taint_audit_events_artifact_id",
        "taint_audit_events",
        ["artifact_id"],
    )
    op.create_index(
        "idx_taint_audit_turn_event_type",
        "taint_audit_events",
        ["turn_id", "event_type"],
    )
    op.create_index(
        "idx_taint_audit_conversation_created",
        "taint_audit_events",
        ["conversation_id", "created_at"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("idx_taint_audit_conversation_created", "taint_audit_events")
    op.drop_index("idx_taint_audit_turn_event_type", "taint_audit_events")
    op.drop_index("ix_taint_audit_events_artifact_id", "taint_audit_events")
    op.drop_index("ix_taint_audit_events_mode", "taint_audit_events")
    op.drop_index("ix_taint_audit_events_effective_outcome", "taint_audit_events")
    op.drop_index("ix_taint_audit_events_requested_outcome", "taint_audit_events")
    op.drop_index("ix_taint_audit_events_max_tier", "taint_audit_events")
    op.drop_index("ix_taint_audit_events_sink_class", "taint_audit_events")
    op.drop_index("ix_taint_audit_events_tool_call_id", "taint_audit_events")
    op.drop_index("ix_taint_audit_events_tool_name", "taint_audit_events")
    op.drop_index("ix_taint_audit_events_subconversation_id", "taint_audit_events")
    op.drop_index("ix_taint_audit_events_processing_profile_id", "taint_audit_events")
    op.drop_index("ix_taint_audit_events_turn_id", "taint_audit_events")
    op.drop_index("ix_taint_audit_events_conversation_id", "taint_audit_events")
    op.drop_index("ix_taint_audit_events_event_type", "taint_audit_events")
    op.drop_index("ix_taint_audit_events_created_at", "taint_audit_events")
    op.drop_table("taint_audit_events")
