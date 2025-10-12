"""
Handles storage for schedule-based automations (recurring actions).
"""

import logging

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
from family_assistant.storage.events import EventActionType, InterfaceType

logger = logging.getLogger(__name__)


# Define the schedule automations table
schedule_automations_table = Table(
    "schedule_automations",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(100), nullable=False),
    Column("description", Text, nullable=True),
    Column("conversation_id", String(255), nullable=False, index=True),
    Column(
        "interface_type",
        SQLEnum(InterfaceType),
        nullable=False,
        server_default="telegram",
    ),
    # Schedule-specific trigger configuration
    Column("recurrence_rule", Text, nullable=False),  # RRULE string
    Column("next_scheduled_at", DateTime(timezone=True), nullable=True),
    # Action configuration
    Column(
        "action_type",
        SQLEnum(EventActionType),
        nullable=False,
        server_default="wake_llm",
    ),
    Column(
        "action_config",
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
    ),
    # Management fields
    Column("enabled", Boolean, nullable=False, server_default="true"),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),  # pylint: disable=not-callable
    ),
    Column("last_execution_at", DateTime(timezone=True), nullable=True),
    Column("execution_count", Integer, nullable=False, server_default="0"),
    # Constraints and indexes
    UniqueConstraint("name", "conversation_id", name="uq_sched_name_conversation"),
    Index("idx_sched_conversation", "conversation_id", "enabled"),
    Index("idx_sched_next", "next_scheduled_at", "enabled"),
)
