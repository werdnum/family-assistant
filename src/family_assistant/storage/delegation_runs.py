"""Storage table for asynchronous profile delegation runs."""

from sqlalchemy import JSON, Column, DateTime, Integer, String, Table, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import functions as func

from family_assistant.storage.base import metadata

delegation_runs_table = Table(
    "delegation_runs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("delegation_id", String(100), nullable=False, unique=True, index=True),
    Column("task_id", String(100), nullable=False, unique=True, index=True),
    Column("status", String(50), nullable=False, index=True),
    Column("source_profile_id", String(100), nullable=False, index=True),
    Column("target_service_id", String(100), nullable=False, index=True),
    Column("interface_type", String(50), nullable=False, index=True),
    Column("conversation_id", String(255), nullable=False, index=True),
    Column("user_id", String(255), nullable=True),
    Column("user_name", String(255), nullable=True),
    Column("source_turn_id", String(100), nullable=True),
    Column("source_tool_call_id", String(255), nullable=True),
    Column("subconversation_id", String(36), nullable=False, index=True),
    Column("request_text", Text, nullable=False),
    Column(
        "content_parts_json", JSON().with_variant(JSONB, "postgresql"), nullable=False
    ),
    Column(
        "attachment_ids_json",
        JSON().with_variant(JSONB, "postgresql"),
        nullable=True,
    ),
    Column("handoff_after_at", DateTime(timezone=True), nullable=False),
    Column("handed_off_at", DateTime(timezone=True), nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=True, onupdate=func.now()),
    Column("result_text", Text, nullable=True),
    Column(
        "result_attachment_ids_json",
        JSON().with_variant(JSONB, "postgresql"),
        nullable=True,
    ),
    Column("result_message_internal_id", Integer, nullable=True),
    Column("error", Text, nullable=True),
    Column("notified_at", DateTime(timezone=True), nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)
