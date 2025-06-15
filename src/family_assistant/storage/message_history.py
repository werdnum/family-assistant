"""
Handles storage and retrieval of message history.
"""

import json  # Added json import
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, cast  # Added Tuple, cast

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Integer,  # Import Integer
    Select,  # Used in cast() for type checking
    String,
    Table,
    Text,
    insert,
    select,
    update,  # Add update import
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError

from family_assistant.storage.base import metadata
from family_assistant.storage.context import DatabaseContext

if TYPE_CHECKING:
    from sqlalchemy.engine import Result

logger = logging.getLogger(__name__)

# Define the message history table
message_history_table = Table(
    "message_history",
    metadata,
    Column(
        "internal_id", Integer, primary_key=True, autoincrement=True
    ),  # Changed to Integer for SQLite compatibility
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
    ),  # internal_id of the first message in the conversation thread
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
    Column(
        "processing_profile_id", String(255), nullable=True, index=True
    ),  # ID of the processing profile used
)


async def add_message_to_history(
    db_context: DatabaseContext,  # Added context
    # --- New/Renamed Parameters ---
    interface_type: str,
    conversation_id: str,
    interface_message_id: str | None,  # Can be None for agent-generated messages
    turn_id: str | None,
    thread_root_id: int | None,  # Added thread_root_id
    timestamp: datetime,
    role: str,  # 'user', 'assistant', 'system', 'tool', 'error'
    content: str | None,  # Content can be optional now
    # --- Renamed/Added Fields ---
    tool_calls: list[dict[str, Any]] | None = None,  # Renamed from tool_calls_info
    reasoning_info: dict[str, Any] | None = None,  # Added
    # Note: `tool_call_id` is now a separate parameter below for 'tool' role messages
    error_traceback: str | None = None,  # Added
    tool_call_id: (
        str | None
    ) = None,  # Added: ID linking tool response to assistant request
    processing_profile_id: str | None = None,  # Added: Profile ID
) -> dict[str, Any] | None:  # Changed to return Optional[Dict]
    """Adds a message to the history table, including optional fields."""
    # Note: The return type was previously Optional[int], changed to Optional[Dict] to return ID in a dict

    # Pre-serialization check for JSON fields
    json_fields_to_check = {
        "tool_calls": tool_calls,
        "reasoning_info": reasoning_info,
    }
    for field_name, field_value in json_fields_to_check.items():
        if field_value is not None:
            try:
                json.dumps(field_value)
            except TypeError as te:
                error_message = (
                    f"Data for field '{field_name}' in add_message_to_history is not JSON serializable. "
                    f"Value type: {type(field_value)}. Value snippet (first 200 chars): {str(field_value)[:200]}. "
                    f"Original error: {te}"
                )
                logger.error(error_message, exc_info=True)
                # Raise a new TypeError with detailed info, making it easier to catch upstream if needed,
                # or to provide a clearer error log.
                raise TypeError(error_message) from te

    try:
        stmt = (  # Start statement assignment
            insert(message_history_table)  # Call insert
            .values(  # Specify values to insert
                interface_type=interface_type,  # Added missing interface_type
                conversation_id=conversation_id,
                interface_message_id=interface_message_id,
                turn_id=turn_id,
                thread_root_id=thread_root_id,
                timestamp=timestamp,
                role=role,
                content=content,
                # Renamed tool_calls_info to tool_calls
                tool_calls=tool_calls,
                reasoning_info=reasoning_info,
                error_traceback=error_traceback,
                tool_call_id=tool_call_id,
                processing_profile_id=processing_profile_id,  # Store profile ID
            )  # Close .values()
            .returning(message_history_table.c.internal_id)  # Specify returning clause
        )  # Close statement assignment parenthesis
        # Use execute_with_retry as commit is handled by context manager
        result: Result = await db_context.execute_with_retry(stmt)
        internal_id = result.scalar_one_or_none()
        # Log after successful insertion before returning
        # Ideally return the full row, but returning just the ID for now
        return {"internal_id": internal_id} if internal_id else None
    except SQLAlchemyError as e:
        logger.error(
            f"Database error in add_message_to_history for conv {interface_type}:{conversation_id}: {e}",
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
        return result.rowcount > 0  # type: ignore[attr-defined]
    except SQLAlchemyError as e:
        logger.error(
            f"Database error in update_message_interface_id(internal_id={internal_id}): {e}",
            exc_info=True,
        )
        raise


async def update_message_error_traceback(
    db_context: DatabaseContext, internal_id: int, error_traceback: str
) -> bool:
    """
    Updates the error_traceback field for a specific message by its internal ID.

    Args:
        db_context: The database context.
        internal_id: The internal_id of the message to update.
        error_traceback: The error traceback string to store.

    Returns:
        True if the update was successful, False otherwise.
    """
    stmt = (
        update(message_history_table)
        .where(message_history_table.c.internal_id == internal_id)
        .values(error_traceback=error_traceback)
    )
    result = await db_context.execute_with_retry(stmt)
    return result.rowcount > 0 if result else False


async def get_recent_history(
    db_context: DatabaseContext,  # Added context
    # --- New Parameters ---
    interface_type: str,
    conversation_id: str,
    limit: int,
    max_age: timedelta,
    processing_profile_id: str | None = None,  # Added for filtering
) -> list[dict[str, Any]]:
    """Retrieves recent messages for a conversation, including tool call info.
    If a message included by limit/max_age belongs to a turn, all other messages
    from that turn for the same conversation are also included, even if they
    would otherwise be outside the limit/max_age.
    """
    try:
        cutoff_time = datetime.now(timezone.utc) - max_age
        # Define the columns to select for consistency
        selected_columns = [
            message_history_table.c.internal_id,
            message_history_table.c.interface_type,
            message_history_table.c.conversation_id,
            message_history_table.c.interface_message_id,
            message_history_table.c.turn_id,
            message_history_table.c.thread_root_id,
            message_history_table.c.timestamp,
            message_history_table.c.role,
            message_history_table.c.content,
            message_history_table.c.tool_calls,
            message_history_table.c.reasoning_info,
            message_history_table.c.error_traceback,
            message_history_table.c.tool_call_id,
            message_history_table.c.processing_profile_id,  # Added profile ID
        ]

        # Step 1: Initial fetch of candidate messages based on limit and max_age
        stmt_candidates = (
            select(*selected_columns)
            .where(message_history_table.c.interface_type == interface_type)
            .where(message_history_table.c.conversation_id == conversation_id)
            .where(message_history_table.c.timestamp >= cutoff_time)
        )
        if processing_profile_id:
            stmt_candidates = stmt_candidates.where(
                message_history_table.c.processing_profile_id == processing_profile_id
            )
        stmt_candidates = stmt_candidates.order_by(
            message_history_table.c.timestamp.desc(),
            message_history_table.c.internal_id.desc(),  # Add this secondary sort
        ).limit(limit)
        candidate_rows_result = await db_context.fetch_all(
            cast("Select[Any]", stmt_candidates)
        )
        # Store candidate messages in a dictionary by internal_id for easy merging
        # These are newest first at this stage.
        all_messages_dict: dict[int, dict[str, Any]] = {
            # Use public item access for 'internal_id' instead of relying on ._mapping internal attribute
            row_mapping["internal_id"]: dict(row_mapping)
            for row_mapping in candidate_rows_result
        }

        # Step 2: Collect unique turn_ids from candidate messages
        turn_ids_to_expand = {
            msg["turn_id"] for msg in all_messages_dict.values() if msg.get("turn_id")
        }

        # Step 3: Fetch full turns for these turn_ids
        if turn_ids_to_expand:
            logger.debug(f"Expanding history for turn_ids: {turn_ids_to_expand}")
            stmt_expand_turns = (
                select(*selected_columns)
                .where(message_history_table.c.interface_type == interface_type)
                .where(message_history_table.c.conversation_id == conversation_id)
                .where(message_history_table.c.turn_id.in_(turn_ids_to_expand))
            )
            # Also filter expanded turns by profile ID if provided
            if processing_profile_id:
                stmt_expand_turns = stmt_expand_turns.where(
                    message_history_table.c.processing_profile_id
                    == processing_profile_id
                )
            # We fetch all messages for these turns, even if some parts of the turn
            # are older than cutoff_time, as one part of the turn met the criteria.
            expanded_turn_rows_result = await db_context.fetch_all(
                cast("Select[Any]", stmt_expand_turns)
            )

            for row_mapping in expanded_turn_rows_result:
                msg_dict = dict(row_mapping)
                # Add or update in all_messages_dict. This handles duplicates if a message
                # was in both candidate set and expanded set.
                all_messages_dict[msg_dict["internal_id"]] = msg_dict

        # Step 4: Combine and Finalize
        # Convert dict values to list
        final_message_list = list(all_messages_dict.values())

        # Sort chronologically (oldest first for the final list)
        final_message_list.sort(key=lambda x: (x["timestamp"], x["internal_id"]))

        return final_message_list
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
) -> dict[str, Any] | None:
    """Retrieves a specific message by its chat and message ID, including all fields."""
    try:
        # Select all columns explicitly to include the new one if table object isn't updated dynamically in all contexts
        selected_columns = [col for col in message_history_table.c]
        stmt = (
            select(*selected_columns)  # Select all columns
            # Filter by new identifiers
            .where(message_history_table.c.interface_type == interface_type)
            .where(message_history_table.c.conversation_id == conversation_id)
            .where(message_history_table.c.interface_message_id == interface_message_id)
        )
        row = await db_context.fetch_one(
            cast("Select[Any]", stmt)
        )  # Cast for type checker
        return dict(row) if row else None  # Return full row as dict
    except SQLAlchemyError as e:
        logger.error(
            f"Database error in get_message_by_interface_id({interface_type}, {conversation_id}, {interface_message_id}): {e}",
            exc_info=True,
        )
        raise


# --- New Functions ---
async def get_messages_by_turn_id(
    db_context: DatabaseContext, turn_id: str
) -> list[dict[str, Any]]:
    """Retrieves all messages associated with a specific turn ID."""
    try:
        selected_columns = [col for col in message_history_table.c]
        stmt = (
            select(*selected_columns)  # Select all columns
            .where(message_history_table.c.turn_id == turn_id)
            .order_by(
                message_history_table.c.internal_id
            )  # Order by insertion sequence first
        )  # Close the statement parenthesis
        rows = await db_context.fetch_all(
            cast("Select[Any]", stmt)
        )  # Cast for type checker
        return [dict(row) for row in rows]
    except SQLAlchemyError as e:
        logger.error(
            f"Database error in get_messages_by_turn_id(turn_id={turn_id}): {e}",
            exc_info=True,
        )
        raise


async def get_messages_by_thread_id(
    db_context: DatabaseContext,
    thread_root_id: int,
    processing_profile_id: str | None = None,  # Added for filtering
) -> list[dict[str, Any]]:
    """Retrieves all messages belonging to a specific conversation thread."""
    # A thread is defined by the `internal_id` of its first message.
    # Messages in the thread either have `thread_root_id` pointing to that first message,
    # or *are* the first message (where `internal_id == thread_root_id` would be true,
    try:
        # although the first message itself has `thread_root_id` as NULL).
        # Corrected query to include the root message itself
        selected_columns = [col for col in message_history_table.c]
        stmt = select(
            *selected_columns
        ).where(  # Filter by thread root ID or the root message itself
            (message_history_table.c.thread_root_id == thread_root_id)
            | (message_history_table.c.internal_id == thread_root_id)
        )
        if processing_profile_id:
            stmt = stmt.where(
                message_history_table.c.processing_profile_id == processing_profile_id
            )
        stmt = stmt.order_by(
            message_history_table.c.internal_id
        )  # Order by insertion sequence first
        rows = await db_context.fetch_all(
            cast("Select[Any]", stmt)
        )  # Cast for type checker
        return [dict(row) for row in rows]
    except SQLAlchemyError as e:
        logger.error(
            f"Database error in get_messages_by_thread_id(thread_root_id={thread_root_id}): {e}",
            exc_info=True,
        )
        raise


async def get_grouped_message_history(
    db_context: DatabaseContext,  # Added context
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Retrieves all message history, grouped by (interface_type, conversation_id) and ordered by timestamp."""
    try:
        selected_columns = [col for col in message_history_table.c]
        stmt = select(*selected_columns).order_by(  # Select all columns
            message_history_table.c.interface_type,
            # Group by (interface_type, conversation_id) tuple
            message_history_table.c.conversation_id,
            message_history_table.c.timestamp,  # Order chronologically within group
            message_history_table.c.internal_id,  # Add for stable chronological order
        )
        rows = await db_context.fetch_all(
            cast("Select[Any]", stmt)
        )  # Cast for type checker
        # Convert RowMapping to dicts for easier handling
        dict_rows = [dict(row) for row in rows]
        # Initialize with the correct type annotation
        grouped_history: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row_dict in dict_rows:  # Iterate over dictionaries
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
