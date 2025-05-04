"""
Handles storage and retrieval of message history.
"""

import logging
import random
import asyncio
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Tuple # Added Tuple

from sqlalchemy import (
    Table,
    Column,
    Integer, # Import Integer
    String,
    BigInteger,
    DateTime,
    Text, Select, # Add Select
    update, # Add update import
    JSON,
    select,
    insert,
    desc,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.engine import Result # Import Result for type hint
from sqlalchemy.dialects.postgresql import JSONB

from family_assistant.storage.base import metadata

from family_assistant.storage.context import DatabaseContext

logger = logging.getLogger(__name__)

# Define the message history table
message_history_table = Table(
    "message_history",
    metadata,
    Column("internal_id", Integer, primary_key=True, autoincrement=True), # Changed to Integer for SQLite compatibility
    Column(
        "interface_type", String(50), nullable=False, index=True
    ),  # e.g., 'telegram', 'web', 'email'
    Column(
        "conversation_id", String(255), nullable=False, index=True
    ),  # e.g., Telegram chat ID string, web session UUID
    Column(
        "interface_message_id", String(255), nullable=True, index=True
    ),  # e.g., Telegram message ID string
    Column(
        "turn_id", String(36), nullable=True, index=True
    ),  # UUID linking messages within a turn
    Column(
        "thread_root_id", BigInteger, nullable=True, index=True
    ), # internal_id of the first message in the conversation thread
    Column("timestamp", DateTime(timezone=True), nullable=False, index=True),
    Column(
        "role", String, nullable=False
    ),  # 'user', 'assistant', 'system', 'tool', 'error'
    Column(
        "content", Text, nullable=True
    ),  # Allow null content for tool/error messages potentially
    Column("tool_calls", JSON().with_variant(JSONB, "postgresql"), nullable=True),
    Column(
        "reasoning_info", JSON().with_variant(JSONB, "postgresql"), nullable=True
    ),  # For assistant messages, LLM reasoning/usage
    Column(
        "tool_call_id", String, nullable=True, index=True
    ),  # For tool role messages, linking to assistant request
    Column(
        "error_traceback", Text, nullable=True
    ),  # For error messages or messages causing errors
)


async def add_message_to_history(
    db_context: DatabaseContext,  # Added context
    # --- New/Renamed Parameters ---
    interface_type: str,
    conversation_id: str,
    interface_message_id: Optional[str], # Can be None for agent-generated messages
    turn_id: Optional[str],
    thread_root_id: Optional[int], # Added thread_root_id
    timestamp: datetime,
    role: str,  # 'user', 'assistant', 'system', 'tool', 'error'
    content: Optional[str],  # Content can be optional now
    # --- Renamed/Added Fields ---
    tool_calls: Optional[List[Dict[str, Any]]] = None, # Renamed from tool_calls_info
    reasoning_info: Optional[Dict[str, Any]] = None,  # Added
    # Note: `tool_call_id` is now a separate parameter below for 'tool' role messages
    error_traceback: Optional[str] = None,  # Added
    tool_call_id: Optional[
        str
    ] = None,  # Added: ID linking tool response to assistant request
) -> Optional[Dict[str, Any]]: # Changed to return Optional[Dict]
    """Adds a message to the history table, including optional tool call info, reasoning, error, and tool_call_id."""
    try:
        stmt = insert(message_history_table).values(
            interface_type=interface_type,
            conversation_id=conversation_id,
            interface_message_id=interface_message_id,
            turn_id=turn_id,
            thread_root_id=thread_root_id, # Added
            timestamp=timestamp,
            role=role,
            content=content,
            # Renamed tool_calls_info to tool_calls
            tool_calls=tool_calls, # Renamed
            reasoning_info=reasoning_info,  # Added
            error_traceback=error_traceback,  # Added
            tool_call_id=tool_call_id,  # Added
        ).returning(message_history_table.c.internal_id) # Return internal_id
        # Use execute_with_retry as commit is handled by context manager
        result: Result = await db_context.execute_with_retry(stmt)
        internal_id = result.scalar_one_or_none()
        logger.debug(
            f"Added message (interface_id={interface_message_id}) for conversation "
            f"{interface_type}:{conversation_id} (turn={turn_id}, thread={thread_root_id}) to history."
        )
        # Return a dict containing the ID, or None
        # Ideally return the full row, but returning just the ID for now
        return {"internal_id": internal_id} if internal_id else None
    except SQLAlchemyError as e:
        logger.error(
            f"Database error in add_message_to_history({interface_type}, {conversation_id}, {interface_message_id}): {e}",
            exc_info=True,
        )
        raise


async def update_message_interface_id(
    db_context: DatabaseContext, internal_id: int, interface_message_id: str
) -> bool:
    """Updates the interface_message_id for a message identified by its internal_id."""
    try:
        stmt = (
            update(message_history_table)
            .where(message_history_table.c.internal_id == internal_id)
            .values(interface_message_id=interface_message_id)
        )
        result: Result = await db_context.execute_with_retry(stmt)
        return result.rowcount > 0
    except SQLAlchemyError as e:
        logger.error(
            f"Database error in update_message_interface_id(internal_id={internal_id}): {e}",
            exc_info=True,
        )
        raise


async def get_recent_history(
    db_context: DatabaseContext,  # Added context
    # --- New Parameters ---
    interface_type: str,
    conversation_id: str,
    limit: int,
    max_age: timedelta,
) -> List[Dict[str, Any]]:
    """Retrieves recent messages for a conversation, including tool call info."""
    try:
        cutoff_time = datetime.now(timezone.utc) - max_age
        stmt = (
            select(
                # Select all relevant columns based on the new schema
                message_history_table.c.internal_id,
                message_history_table.c.interface_type,
                message_history_table.c.conversation_id,
                message_history_table.c.interface_message_id,
                message_history_table.c.turn_id,
                message_history_table.c.thread_root_id,
                message_history_table.c.timestamp,
                message_history_table.c.role,
                message_history_table.c.content,
                message_history_table.c.tool_calls, # Renamed
                message_history_table.c.reasoning_info,  # Added
                message_history_table.c.error_traceback,  # Added
                message_history_table.c.tool_call_id,  # Added
            )
            # Filter by new conversation identifiers
            .where(message_history_table.c.interface_type == interface_type)
            .where(message_history_table.c.conversation_id == conversation_id)
            .where(message_history_table.c.timestamp >= cutoff_time)
            # Order by internal_id for strict insertion order tie-breaking
            .order_by(message_history_table.c.timestamp.desc())
            .limit(limit)
        )
        rows = await db_context.fetch_all(cast(Select, stmt)) # Cast for type checker
        # Convert rows to dicts and reverse to get chronological order
        formatted_rows = [dict(row) for row in reversed(rows)]
        return formatted_rows
    except SQLAlchemyError as e:
        logger.error(
            f"Database error in get_recent_history({interface_type}, {conversation_id}): {e}",
            exc_info=True,
        )
        raise


# --- New Functions ---

async def get_message_by_interface_id(
    db_context: DatabaseContext,  # Added context
    # --- New Parameters ---
    interface_type: str,
    conversation_id: str,
    interface_message_id: str,
) -> Optional[Dict[str, Any]]:
    """Retrieves a specific message by its chat and message ID, including all fields."""
    try:
        stmt = (
            select(message_history_table)  # Select all columns
            # Filter by new identifiers
            .where(message_history_table.c.interface_type == interface_type)
            .where(message_history_table.c.conversation_id == conversation_id)
            .where(message_history_table.c.interface_message_id == interface_message_id)
        )
        row = await db_context.fetch_one(cast(Select, stmt)) # Cast for type checker
        return dict(row) if row else None  # Return full row as dict
    except SQLAlchemyError as e:
        logger.error(
            f"Database error in get_message_by_interface_id({interface_type}, {conversation_id}, {interface_message_id}): {e}",
            exc_info=True,
        )
        raise


# --- New Functions Implemented ---
async def get_messages_by_turn_id(
    db_context: DatabaseContext, turn_id: str
) -> List[Dict[str, Any]]:
    """Retrieves all messages associated with a specific turn ID."""
    try:
        stmt = (
            select(message_history_table)
            .where(message_history_table.c.turn_id == turn_id)
            .order_by(message_history_table.c.internal_id)  # Order by insertion sequence
        )
        rows = await db_context.fetch_all(cast(Select, stmt)) # Cast for type checker
        return [dict(row) for row in rows]
    except SQLAlchemyError as e:
        logger.error(
            f"Database error in get_messages_by_turn_id(turn_id={turn_id}): {e}",
            exc_info=True,
        )
        raise


async def get_messages_by_thread_id(
    db_context: DatabaseContext, thread_root_id: int
) -> List[Dict[str, Any]]:
    """Retrieves all messages belonging to a specific conversation thread."""
    try:
        stmt = (
            select(message_history_table)
            .where(message_history_table.c.thread_root_id == thread_root_id)
            .order_by(message_history_table.c.internal_id)  # Order by insertion sequence
        )
        rows = await db_context.fetch_all(cast(Select, stmt)) # Cast for type checker
        return [dict(row) for row in rows]
    except SQLAlchemyError as e:
        logger.error(
            f"Database error in get_messages_by_thread_id(thread_root_id={thread_root_id}): {e}",
            exc_info=True,
        )
        raise

async def get_grouped_message_history(
    db_context: DatabaseContext,  # Added context
) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    """Retrieves all message history, grouped by (interface_type, conversation_id) and ordered by timestamp."""
    try:
        stmt = select(message_history_table).order_by(  # Select all columns
            message_history_table.c.interface_type,
            # Group by (interface_type, conversation_id) tuple
            message_history_table.c.conversation_id,
            message_history_table.c.timestamp, # Order chronologically within group
        )
        rows = await db_context.fetch_all(cast(Select, stmt)) # Cast for type checker
        # Convert RowMapping to dicts for easier handling
        dict_rows = [dict(row) for row in rows]
        grouped_history = {}
        for row_dict in dict_rows: # Iterate over dictionaries
            group_key = (row_dict["interface_type"], row_dict["conversation_id"])
            if group_key not in grouped_history:
                grouped_history[group_key] = []
            grouped_history[group_key].append(row_dict)
        return grouped_history
    except SQLAlchemyError as e:
        logger.error(
            f"Database error in get_grouped_message_history: {e}",
            exc_info=True,
        )
        raise
