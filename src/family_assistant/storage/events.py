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
)
from sqlalchemy import (
    Enum as SQLEnum,
)
from sqlalchemy.dialects.postgresql import JSONB

from family_assistant.storage.base import metadata
from family_assistant.storage.context import DatabaseContext

logger = logging.getLogger(__name__)


# Enum types for the event system
class EventSourceType(str, Enum):
    """Types of event sources."""

    HOME_ASSISTANT = "home_assistant"
    INDEXING = "indexing"
    WEBHOOK = "webhook"


class EventActionType(str, Enum):
    """Types of actions that can be triggered by events."""

    WAKE_LLM = "wake_llm"


class InterfaceType(str, Enum):
    """Types of interfaces that can receive event notifications."""

    TELEGRAM = "telegram"
    WEB = "web"
    EMAIL = "email"


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
        server_default="CURRENT_TIMESTAMP",
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
        server_default="CURRENT_TIMESTAMP",
    ),
    # Indexes for efficient querying
    Index("idx_source_time", "source_id", "timestamp"),
    Index("idx_created", "created_at"),  # For efficient cleanup
)


async def check_and_update_rate_limit(
    db_context: DatabaseContext, listener: dict[str, Any]
) -> tuple[bool, str | None]:
    """Check rate limit and update counter atomically."""
    now = datetime.now(timezone.utc)

    # Check if we need to reset daily counter
    if not listener["daily_reset_at"] or now > listener["daily_reset_at"]:
        # Reset counter for new day
        tomorrow = now.replace(hour=0, minute=0, second=0) + timedelta(days=1)
        await db_context.execute(
            """UPDATE event_listeners 
               SET daily_executions = 1, 
                   daily_reset_at = ?, 
                   last_execution_at = ?
               WHERE id = ?""",
            [tomorrow, now, listener["id"]],
        )
        return True, None

    # Check if under limit
    if listener["daily_executions"] >= 5:
        return (
            False,
            f"Daily limit exceeded ({listener['daily_executions']} triggers today)",
        )

    # Increment counter
    await db_context.execute(
        """UPDATE event_listeners 
           SET daily_executions = daily_executions + 1,
               last_execution_at = ?
           WHERE id = ?""",
        [now, listener["id"]],
    )

    return True, None
