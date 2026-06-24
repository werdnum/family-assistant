"""Storage table for asynchronous profile delegation runs."""

from typing import Literal

from sqlalchemy import JSON, Column, DateTime, Index, Integer, String, Table, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import functions as func

from family_assistant.storage.base import metadata

DelegationRunStatus = Literal[
    "queued",
    "running",
    "awaiting_remote",
    "completed",
    "failed",
]

TERMINAL_DELEGATION_STATUSES: frozenset[DelegationRunStatus] = frozenset({
    "completed",
    "failed",
})

delegation_runs_table = Table(
    "delegation_runs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("delegation_id", String(100), nullable=False, unique=True, index=True),
    Column("task_id", String(100), nullable=False, unique=True),
    Column("status", String(50), nullable=False),
    Column("source_profile_id", String(100), nullable=False),
    Column("target_service_id", String(100), nullable=False),
    Column("interface_type", String(50), nullable=False),
    Column("conversation_id", String(255), nullable=False),
    Column("user_id", String(255), nullable=True),
    Column("user_name", String(255), nullable=True),
    Column("source_turn_id", String(100), nullable=True),
    Column("source_subconversation_id", String(36), nullable=True),
    Column("subconversation_id", String(36), nullable=False),
    Column("request_text", Text, nullable=False),
    Column(
        "content_parts_json", JSON().with_variant(JSONB, "postgresql"), nullable=False
    ),
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
    # Remote (A2A) task identifiers for the submit-then-poll async path. Null
    # for local delegations, which have no remote task to poll.
    Column("remote_task_id", String(255), nullable=True),
    Column("remote_context_id", String(255), nullable=True),
    Column("poll_attempts", Integer, nullable=False, server_default="0"),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Index(
        "ix_delegation_runs_conversation_created",
        "conversation_id",
        "created_at",
    ),
    Index(
        "ix_delegation_runs_status_created",
        "status",
        "created_at",
    ),
)
