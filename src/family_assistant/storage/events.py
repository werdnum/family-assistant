"""
Handles storage for the event listener system.
"""

import logging
from datetime import datetime, timedelta, timezone
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
    delete,
    insert,
    select,
    update,
)
from sqlalchemy import (
    Enum as SQLEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
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
    try:
        stmt = insert(event_listeners_table).values(
            name=name,
            description=description,
            source_id=source_id,
            match_conditions=match_conditions,
            action_type=EventActionType.wake_llm,  # Default for now
            action_config=action_config,
            conversation_id=conversation_id,
            interface_type=interface_type,
            one_time=one_time,
            enabled=enabled,
            created_at=datetime.now(timezone.utc),
            daily_executions=0,
        )

        result = await db_context.execute_with_retry(stmt)
        listener_id = result.lastrowid  # type: ignore[attr-defined]

        logger.info(
            f"Created event listener '{name}' (ID: {listener_id}) for conversation {conversation_id}"
        )
        return listener_id

    except IntegrityError as e:
        if "uq_name_conversation" in str(e):
            logger.error(
                f"Event listener with name '{name}' already exists for conversation {conversation_id}"
            )
            raise ValueError(
                f"An event listener named '{name}' already exists in this conversation"
            ) from e
        raise
    except SQLAlchemyError as e:
        logger.error(f"Database error in create_event_listener: {e}", exc_info=True)
        raise


async def get_event_listeners(
    db_context: DatabaseContext,
    conversation_id: str,
    source_id: str | None = None,
    enabled: bool | None = None,
) -> list[dict[str, Any]]:
    """Get event listeners for a conversation with optional filters."""
    try:
        # Start with base query filtered by conversation_id
        stmt = select(event_listeners_table).where(
            event_listeners_table.c.conversation_id == conversation_id
        )

        # Apply optional filters
        if source_id is not None:
            stmt = stmt.where(event_listeners_table.c.source_id == source_id)
        if enabled is not None:
            stmt = stmt.where(event_listeners_table.c.enabled == enabled)

        # Order by creation date, newest first
        stmt = stmt.order_by(event_listeners_table.c.created_at.desc())

        rows = await db_context.fetch_all(stmt)

        # Convert rows to dicts
        listeners = []
        for row in rows:
            listener = dict(row)
            listeners.append(listener)

        return listeners

    except SQLAlchemyError as e:
        logger.error(f"Database error in get_event_listeners: {e}", exc_info=True)
        raise


async def get_event_listener_by_id(
    db_context: DatabaseContext,
    listener_id: int,
    conversation_id: str,
) -> dict[str, Any] | None:
    """Get a specific listener, ensuring it belongs to the conversation."""
    try:
        stmt = select(event_listeners_table).where(
            (event_listeners_table.c.id == listener_id)
            & (event_listeners_table.c.conversation_id == conversation_id)
        )

        row = await db_context.fetch_one(stmt)
        if not row:
            return None

        # Convert to dict
        listener = dict(row)
        return listener

    except SQLAlchemyError as e:
        logger.error(
            f"Database error in get_event_listener_by_id({listener_id}): {e}",
            exc_info=True,
        )
        raise


async def update_event_listener_enabled(
    db_context: DatabaseContext,
    listener_id: int,
    conversation_id: str,
    enabled: bool,
) -> bool:
    """Toggle listener enabled status."""
    try:
        stmt = (
            update(event_listeners_table)
            .where(
                (event_listeners_table.c.id == listener_id)
                & (event_listeners_table.c.conversation_id == conversation_id)
            )
            .values(enabled=enabled)
        )

        result = await db_context.execute_with_retry(stmt)
        updated_count = result.rowcount  # type: ignore[attr-defined]

        if updated_count > 0:
            status = "enabled" if enabled else "disabled"
            logger.info(f"Updated event listener {listener_id} to {status}")
            return True
        else:
            logger.warning(
                f"Event listener {listener_id} not found for conversation {conversation_id}"
            )
            return False

    except SQLAlchemyError as e:
        logger.error(
            f"Database error in update_event_listener_enabled({listener_id}): {e}",
            exc_info=True,
        )
        raise


async def delete_event_listener(
    db_context: DatabaseContext,
    listener_id: int,
    conversation_id: str,
) -> bool:
    """Delete a listener."""
    try:
        # First get the listener name for logging
        listener = await get_event_listener_by_id(
            db_context, listener_id, conversation_id
        )
        if not listener:
            logger.warning(
                f"Event listener {listener_id} not found for conversation {conversation_id}"
            )
            return False

        stmt = delete(event_listeners_table).where(
            (event_listeners_table.c.id == listener_id)
            & (event_listeners_table.c.conversation_id == conversation_id)
        )

        result = await db_context.execute_with_retry(stmt)
        deleted_count = result.rowcount  # type: ignore[attr-defined]

        if deleted_count > 0:
            logger.info(
                f"Deleted event listener '{listener['name']}' (ID: {listener_id}) "
                f"for conversation {conversation_id}"
            )
            return True
        else:
            # This shouldn't happen since we checked existence above
            logger.error(
                f"Failed to delete event listener {listener_id} - deletion returned 0 rows"
            )
            return False

    except SQLAlchemyError as e:
        logger.error(
            f"Database error in delete_event_listener({listener_id}): {e}",
            exc_info=True,
        )
        raise


# Additional helper functions for event storage


async def store_event(
    db_context: DatabaseContext,
    source_id: str,
    event_data: dict[str, Any],
    triggered_listener_ids: list[int] | None = None,
    timestamp: datetime | None = None,
) -> None:
    """Store an event in the recent_events table."""
    try:
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)

        # Generate unique event ID
        import time

        event_id = f"{source_id}:{int(time.time() * 1000000)}"

        stmt = insert(recent_events_table).values(
            event_id=event_id,
            source_id=source_id,
            event_data=event_data,
            triggered_listener_ids=triggered_listener_ids,
            timestamp=timestamp,
            created_at=datetime.now(timezone.utc),
        )

        await db_context.execute_with_retry(stmt)

    except SQLAlchemyError as e:
        logger.error(f"Database error in store_event: {e}", exc_info=True)
        # Don't raise - event storage failures shouldn't break event processing


async def query_recent_events(
    db_context: DatabaseContext,
    source_id: str | None = None,
    hours: int = 24,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Query recent events with optional filters."""
    try:
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours)

        stmt = select(recent_events_table).where(
            recent_events_table.c.timestamp >= cutoff_time
        )

        if source_id is not None:
            stmt = stmt.where(recent_events_table.c.source_id == source_id)

        # Order by timestamp descending and apply limit
        stmt = stmt.order_by(recent_events_table.c.timestamp.desc()).limit(limit)

        rows = await db_context.fetch_all(stmt)

        # Convert rows to dicts
        events = []
        for row in rows:
            event = dict(row)
            events.append(event)

        return events

    except SQLAlchemyError as e:
        logger.error(f"Database error in query_recent_events: {e}", exc_info=True)
        raise


async def cleanup_old_events(
    db_context: DatabaseContext,
    retention_hours: int = 48,
) -> int:
    """Clean up events older than retention period."""
    try:
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=retention_hours)

        stmt = delete(recent_events_table).where(
            recent_events_table.c.created_at < cutoff_time
        )

        result = await db_context.execute_with_retry(stmt)
        deleted_count = result.rowcount  # type: ignore[attr-defined]

        logger.info(
            f"Cleaned up {deleted_count} events older than {retention_hours} hours"
        )
        return deleted_count

    except SQLAlchemyError as e:
        logger.error(f"Database error in cleanup_old_events: {e}", exc_info=True)
        raise


# Rate limiting helper functions


async def check_and_update_rate_limit(
    db_context: DatabaseContext,
    listener_id: int,
    conversation_id: str,
) -> tuple[bool, str | None]:
    """Check rate limit and update counter atomically."""
    try:
        now = datetime.now(timezone.utc)

        # Get current listener state
        listener = await get_event_listener_by_id(
            db_context, listener_id, conversation_id
        )
        if not listener:
            return False, "Listener not found"

        # Check if we need to reset daily counter
        if not listener["daily_reset_at"] or now > listener["daily_reset_at"]:
            # Reset counter for new day
            tomorrow = now.replace(
                hour=0, minute=0, second=0, microsecond=0
            ) + timedelta(days=1)
            stmt = (
                update(event_listeners_table)
                .where(event_listeners_table.c.id == listener_id)
                .values(
                    daily_executions=1,
                    daily_reset_at=tomorrow,
                    last_execution_at=now,
                )
            )
            await db_context.execute_with_retry(stmt)
            return True, None

        # Check if under limit
        if listener["daily_executions"] >= 5:
            return (
                False,
                f"Daily limit exceeded ({listener['daily_executions']} triggers today)",
            )

        # Increment counter
        stmt = (
            update(event_listeners_table)
            .where(event_listeners_table.c.id == listener_id)
            .values(
                daily_executions=event_listeners_table.c.daily_executions + 1,
                last_execution_at=now,
            )
        )
        await db_context.execute_with_retry(stmt)

        return True, None

    except SQLAlchemyError as e:
        logger.error(
            f"Database error in check_and_update_rate_limit({listener_id}): {e}",
            exc_info=True,
        )
        # On error, allow execution but log it
        return True, None
