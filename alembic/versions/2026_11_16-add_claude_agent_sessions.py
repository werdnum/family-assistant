"""Add claude_agent_session_entries and claude_agent_session_turns

Revision ID: add_claude_agent_sessions
Revises: add_chatgpt_plan_credentials
Create Date: 2026-11-16 00:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_claude_agent_sessions"
down_revision: str | None = "add_chatgpt_plan_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Store Claude Code sessions so a delegation wake can resume one."""
    op.create_table(
        "claude_agent_session_entries",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("project_key", sa.String(255), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("subpath", sa.String(255), nullable=False),
        sa.Column("entry", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_claude_agent_session_entries_key",
        "claude_agent_session_entries",
        ["project_key", "session_id", "subpath", "id"],
    )
    op.create_index(
        "ix_claude_agent_session_entries_session",
        "claude_agent_session_entries",
        ["session_id", "created_at"],
    )
    op.create_table(
        "claude_agent_session_turns",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("turn_id", sa.String(36), nullable=False, unique=True),
        sa.Column("interface_type", sa.String(50), nullable=False),
        sa.Column("conversation_id", sa.String(255), nullable=False),
        sa.Column("subconversation_id", sa.String(36), nullable=True),
        sa.Column("processing_profile_id", sa.String(255), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("effort", sa.String(32), nullable=True),
        sa.Column("system_prompt_sha256", sa.String(64), nullable=False),
        sa.Column(
            "taint_metadata_json",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_claude_agent_session_turns_session_id",
        "claude_agent_session_turns",
        ["session_id"],
    )
    op.create_index(
        "ix_claude_agent_session_turns_scope",
        "claude_agent_session_turns",
        ["interface_type", "conversation_id", "processing_profile_id", "id"],
    )


def downgrade() -> None:
    """Drop the Claude Code session tables."""
    op.drop_table("claude_agent_session_turns")
    op.drop_table("claude_agent_session_entries")
