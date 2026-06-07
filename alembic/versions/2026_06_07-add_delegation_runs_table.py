"""Add delegation_runs table

Revision ID: add_delegation_runs
Revises: add_ios_push_tokens
Create Date: 2026-06-07 00:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_delegation_runs"
down_revision: str | None = "add_ios_push_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_column_type() -> sa.JSON:
    """Return JSON type with PostgreSQL JSONB variant."""
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "delegation_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("delegation_id", sa.String(length=100), nullable=False),
        sa.Column("task_id", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("source_profile_id", sa.String(length=100), nullable=False),
        sa.Column("target_service_id", sa.String(length=100), nullable=False),
        sa.Column("interface_type", sa.String(length=50), nullable=False),
        sa.Column("conversation_id", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=True),
        sa.Column("user_name", sa.String(length=255), nullable=True),
        sa.Column("source_turn_id", sa.String(length=100), nullable=True),
        sa.Column("source_tool_call_id", sa.String(length=255), nullable=True),
        sa.Column("subconversation_id", sa.String(length=36), nullable=False),
        sa.Column("request_text", sa.Text(), nullable=False),
        sa.Column("content_parts_json", _json_column_type(), nullable=False),
        sa.Column("attachment_ids_json", _json_column_type(), nullable=True),
        sa.Column("handoff_after_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("handed_off_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_text", sa.Text(), nullable=True),
        sa.Column("result_attachment_ids_json", _json_column_type(), nullable=True),
        sa.Column("result_message_internal_id", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_delegation_runs_conversation_id"),
        "delegation_runs",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_delegation_runs_delegation_id"),
        "delegation_runs",
        ["delegation_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_delegation_runs_interface_type"),
        "delegation_runs",
        ["interface_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_delegation_runs_source_profile_id"),
        "delegation_runs",
        ["source_profile_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_delegation_runs_status"),
        "delegation_runs",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_delegation_runs_subconversation_id"),
        "delegation_runs",
        ["subconversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_delegation_runs_target_service_id"),
        "delegation_runs",
        ["target_service_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_delegation_runs_task_id"),
        "delegation_runs",
        ["task_id"],
        unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_delegation_runs_task_id"), table_name="delegation_runs")
    op.drop_index(
        op.f("ix_delegation_runs_target_service_id"),
        table_name="delegation_runs",
    )
    op.drop_index(
        op.f("ix_delegation_runs_subconversation_id"),
        table_name="delegation_runs",
    )
    op.drop_index(op.f("ix_delegation_runs_status"), table_name="delegation_runs")
    op.drop_index(
        op.f("ix_delegation_runs_source_profile_id"),
        table_name="delegation_runs",
    )
    op.drop_index(
        op.f("ix_delegation_runs_interface_type"),
        table_name="delegation_runs",
    )
    op.drop_index(
        op.f("ix_delegation_runs_delegation_id"),
        table_name="delegation_runs",
    )
    op.drop_index(
        op.f("ix_delegation_runs_conversation_id"),
        table_name="delegation_runs",
    )
    op.drop_table("delegation_runs")
