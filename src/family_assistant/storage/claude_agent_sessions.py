"""Storage for Claude Code sessions, kept so a delegation wake can resume one.

``claude_agent_session_entries`` is the Agent SDK's session store: Claude
Code's own transcript lines, as opaque JSON text in append order, read back
only by the CLI's resume. ``claude_agent_session_turns`` records each turn a
session ran in full, with what a wake checks before resuming it. See
docs/design/claude-agent-session-resume.md.
"""

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects import postgresql

from family_assistant.storage.base import metadata

claude_agent_session_entries_table = Table(
    "claude_agent_session_entries",
    metadata,
    Column(
        "id",
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    ),
    Column("project_key", String(255), nullable=False),
    Column("session_id", String(36), nullable=False),
    # The SDK's subpath for a subagent transcript; empty for the main one.
    Column("subpath", String(255), nullable=False),
    Column("entry", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Index(
        "ix_claude_agent_session_entries_key",
        "project_key",
        "session_id",
        "subpath",
        "id",
    ),
    Index("ix_claude_agent_session_entries_session", "session_id", "created_at"),
)

claude_agent_session_turns_table = Table(
    "claude_agent_session_turns",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("session_id", String(36), nullable=False, index=True),
    Column("turn_id", String(36), nullable=False, unique=True),
    Column("interface_type", String(50), nullable=False),
    Column("conversation_id", String(255), nullable=False),
    Column("subconversation_id", String(36), nullable=True),
    Column("processing_profile_id", String(255), nullable=False),
    Column("model", String(255), nullable=False),
    Column("effort", String(32), nullable=True),
    Column("system_prompt_sha256", String(64), nullable=False),
    Column(
        "taint_metadata_json",
        JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
        nullable=True,
    ),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Index(
        "ix_claude_agent_session_turns_scope",
        "interface_type",
        "conversation_id",
        "processing_profile_id",
        "id",
    ),
)
