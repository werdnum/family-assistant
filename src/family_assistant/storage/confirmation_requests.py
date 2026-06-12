"""Storage table for durable tool confirmation requests."""

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects import postgresql

from family_assistant.storage.base import metadata

confirmation_requests_table = Table(
    "confirmation_requests",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("target_user_id", String(255), nullable=False, index=True),
    Column("status", String(32), nullable=False, index=True),
    Column("tool_name", String(255), nullable=False),
    Column(
        "tool_args_json",
        JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
        nullable=False,
    ),
    Column("tool_call_id", String(255), nullable=True),
    Column(
        "source_message_internal_id",
        Integer,
        ForeignKey("message_history.internal_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    ),
    Column("confirmation_prompt", Text, nullable=False),
    # Processing profile that requested the confirmation, so the deferred
    # execution runs under the same profile (script-originated confirmations
    # have no source message row to derive it from).
    Column("processing_profile_id", String(255), nullable=True),
    # Origin interface/conversation of the turn that requested confirmation, so
    # deferred execution can rebuild its context when there is no source message
    # row (automation scripts) instead of falling back to worker defaults.
    Column("origin_interface_type", String(50), nullable=True),
    Column("origin_conversation_id", String(255), nullable=True),
    Column("expires_at", DateTime(timezone=True), nullable=False, index=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("resolved_at", DateTime(timezone=True), nullable=True),
    Column("resolved_by_user_id", String(255), nullable=True),
    Column("resolved_via_interface", String(50), nullable=True),
    Column("execution_task_id", String(255), nullable=True, unique=True),
    CheckConstraint(
        "status IN ('pending', 'approved', 'rejected', 'expired')",
        name="ck_confirmation_requests_status",
    ),
)
