"""Storage table for runtime taint audit events."""

from sqlalchemy import Column, DateTime, Index, String, Table, Text
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import functions as func
from sqlalchemy.types import JSON

from family_assistant.storage.base import metadata

taint_audit_events_table = Table(
    "taint_audit_events",
    metadata,
    Column("event_id", String(36), primary_key=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    ),
    Column("event_type", String(64), nullable=False, index=True),
    Column("conversation_id", String(255), nullable=False, index=True),
    Column("turn_id", String(255), nullable=True, index=True),
    Column("processing_profile_id", String(255), nullable=True, index=True),
    Column("subconversation_id", String(255), nullable=True, index=True),
    Column("tool_name", String(255), nullable=False, index=True),
    Column("tool_call_id", String(255), nullable=True, index=True),
    Column("sink_class", String(64), nullable=True, index=True),
    Column("max_tier", String(64), nullable=False, index=True),
    Column(
        "sources_json",
        JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
        nullable=False,
    ),
    Column("requested_outcome", String(64), nullable=True, index=True),
    Column("effective_outcome", String(64), nullable=True, index=True),
    Column("mode", String(64), nullable=True, index=True),
    Column("reason", Text, nullable=False),
    Column(
        "arguments_summary_json",
        JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
        nullable=True,
    ),
    Column("artifact_id", String(255), nullable=True, index=True),
    Index("idx_taint_audit_turn_event_type", "turn_id", "event_type"),
    Index("idx_taint_audit_conversation_created", "conversation_id", "created_at"),
)
