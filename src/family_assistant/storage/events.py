"""
Handles storage for the event listener system.
"""

import logging
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SQLEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from family_assistant.storage.base import metadata
from family_assistant.storage.context import DatabaseContext

logger = logging.getLogger(__name__)


# Enum types for the event system
class EventSourceType(str, Enum):
    """Types of event sources."""

    home_assistant = "home_assistant"
    indexing = "indexing"
    webhook = "webhook"


class EventActionType(str, Enum):
    """Types of actions that can be triggered by events."""

    wake_llm = "wake_llm"
    script = "script"


class InterfaceType(str, Enum):
    """Types of interfaces that can receive event notifications."""

    telegram = "telegram"
    web = "web"
    email = "email"


# Define the event listeners table
event_listeners_table = Table(
    "event_listeners",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(100), nullable=False),
    Column("description", Text, nullable=True),
    Column(
        "source_id",
        SQLEnum(EventSourceType),
        nullable=False,
        index=True,
    ),
    Column(
        "match_conditions",
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
    ),
    Column(
        "action_type",
        SQLEnum(EventActionType),
        nullable=False,
        server_default="wake_llm",
    ),
    Column(
        "action_config",
        JSON().with_variant(JSONB, "postgresql"),
        nullable=True,
    ),
    Column("conversation_id", String(255), nullable=False, index=True),
    Column(
        "interface_type",
        SQLEnum(InterfaceType),
        nullable=False,
        server_default="telegram",
    ),
    Column("one_time", Boolean, nullable=False, server_default="false"),
    Column("enabled", Boolean, nullable=False, server_default="true"),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),  # pylint: disable=not-callable
    ),
    # Rate limiting fields
    Column("daily_executions", Integer, nullable=False, server_default="0"),
    Column("daily_reset_at", DateTime(timezone=True), nullable=True),
    Column("last_execution_at", DateTime(timezone=True), nullable=True),
    # Constraints and indexes
    UniqueConstraint("name", "conversation_id", name="uq_name_conversation"),
    Index("idx_source_enabled", "source_id", "enabled"),
    Index("idx_conversation", "conversation_id", "enabled"),
)


# Define the recent events table for debugging
recent_events_table = Table(
    "recent_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_id", String(100), nullable=False, unique=True),
    Column(
        "source_id",
        SQLEnum(EventSourceType),
        nullable=False,
        index=True,
    ),
    Column(
        "event_data",
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
    ),
    Column(
        "triggered_listener_ids",
        JSON().with_variant(JSONB, "postgresql"),
        nullable=True,
    ),
    Column("timestamp", DateTime(timezone=True), nullable=False, index=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),  # pylint: disable=not-callable
    ),
    # Indexes for efficient querying
    Index("idx_source_time", "source_id", "timestamp"),
    Index("idx_created", "created_at"),  # For efficient cleanup
)


async def create_event_listener(
    db_context: DatabaseContext,
    name: str,
    source_id: str,
    match_conditions: dict,
    conversation_id: str,
    interface_type: str = "telegram",
    description: str | None = None,
    action_config: dict | None = None,
    one_time: bool = False,
    enabled: bool = True,
) -> int:
    """Create a new event listener, returning its ID."""
    return await db_context.events.create_event_listener(
        name=name,
        source_id=source_id,
        match_conditions=match_conditions,
        conversation_id=conversation_id,
        interface_type=interface_type,
        description=description,
        action_config=action_config,
        one_time=one_time,
        enabled=enabled,
    )


async def get_event_listeners(
    db_context: DatabaseContext,
    conversation_id: str,
    source_id: str | None = None,
    enabled: bool | None = None,
) -> list[dict[str, Any]]:
    """Get event listeners for a conversation with optional filters."""
    return await db_context.events.get_event_listeners(
        conversation_id=conversation_id,
        source_id=source_id,
        enabled=enabled,
    )


async def get_event_listener_by_id(
    db_context: DatabaseContext,
    listener_id: int,
    conversation_id: str,
) -> dict[str, Any] | None:
    """Get a specific listener, ensuring it belongs to the conversation."""
    return await db_context.events.get_event_listener_by_id(
        listener_id=listener_id,
        conversation_id=conversation_id,
    )


async def update_event_listener_enabled(
    db_context: DatabaseContext,
    listener_id: int,
    conversation_id: str,
    enabled: bool,
) -> bool:
    """Toggle listener enabled status."""
    return await db_context.events.update_event_listener_enabled(
        listener_id=listener_id,
        conversation_id=conversation_id,
        enabled=enabled,
    )


async def delete_event_listener(
    db_context: DatabaseContext,
    listener_id: int,
    conversation_id: str,
) -> bool:
    """Delete a listener."""
    return await db_context.events.delete_event_listener(
        listener_id=listener_id,
        conversation_id=conversation_id,
    )


# Additional helper functions for event storage


async def store_event(
    db_context: DatabaseContext,
    source_id: str,
    event_data: dict[str, Any],
    triggered_listener_ids: list[int] | None = None,
    timestamp: datetime | None = None,
) -> None:
    """Store an event in the recent_events table."""
    await db_context.events.store_event(
        source_id=source_id,
        event_data=event_data,
        triggered_listener_ids=triggered_listener_ids,
        timestamp=timestamp,
    )


async def query_recent_events(
    db_context: DatabaseContext,
    source_id: str | None = None,
    hours: int = 24,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Query recent events with optional filters."""
    return await db_context.events.query_recent_events(
        source_id=source_id,
        hours=hours,
        limit=limit,
    )


async def cleanup_old_events(
    db_context: DatabaseContext,
    retention_hours: int = 48,
) -> int:
    """Clean up events older than retention period."""
    return await db_context.events.cleanup_old_events(
        retention_hours=retention_hours,
    )


# Rate limiting helper functions


async def check_and_update_rate_limit(
    db_context: DatabaseContext,
    listener_id: int,
    conversation_id: str,
) -> tuple[bool, str | None]:
    """Check rate limit and update counter atomically."""
    return await db_context.events.check_and_update_rate_limit(
        listener_id=listener_id,
        conversation_id=conversation_id,
    )


# The functions below have been migrated to use the repository pattern
# They remain here for backward compatibility

# Re-export check_and_update_rate_limit as-is since it already uses the repository
# check_and_update_rate_limit is already defined above
