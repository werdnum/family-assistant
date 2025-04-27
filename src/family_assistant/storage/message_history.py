"""
Handles storage and retrieval of message history.
"""

import logging
import random
import asyncio
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional

from sqlalchemy import (
    Table,
    Column,
    String,
    BigInteger,
    DateTime,
    Text,
    JSON,
    select,
    insert,
    desc,
)
from sqlalchemy.exc import SQLAlchemyError # Use broader exception

# Use absolute package path
from family_assistant.storage.base import metadata # Keep metadata
# Remove get_engine import
from family_assistant.storage.context import DatabaseContext # Import DatabaseContext

logger = logging.getLogger(__name__)

# Define the message history table
message_history_table = Table(
    "message_history",
    metadata,
    Column("chat_id", BigInteger, primary_key=True),
    Column("message_id", BigInteger, primary_key=True),
    Column("timestamp", DateTime(timezone=True), nullable=False, index=True),
    Column("role", String, nullable=False),  # 'user' or 'assistant'
    Column("content", Text, nullable=False),
    Column("tool_calls_info", JSON, nullable=True),
)


async def add_message_to_history(
    db_context: DatabaseContext, # Added context
    chat_id: int,
    message_id: int,
    timestamp: datetime,
    role: str,
    content: str,
    tool_calls_info: Optional[List[Dict[str, Any]]] = None,
):
    """Adds a message to the history table, including optional tool call info."""
    try:
        stmt = insert(message_history_table).values(
            chat_id=chat_id,
            message_id=message_id,
            timestamp=timestamp,
            role=role,
            content=content,
            tool_calls_info=tool_calls_info,
        )
        await db_context.execute_and_commit(stmt)
        logger.debug(
            f"Added message {message_id} from chat {chat_id} to history."
        )
    except SQLAlchemyError as e:
        logger.error(
            f"Database error in add_message_to_history({chat_id}, {message_id}): {e}",
            exc_info=True,
        )
        raise


async def get_recent_history(
    db_context: DatabaseContext, # Added context
    chat_id: int,
    limit: int,
    max_age: timedelta
) -> List[Dict[str, Any]]:
    """Retrieves recent messages for a chat, including tool call info."""
    try:
        cutoff_time = datetime.now(timezone.utc) - max_age
        stmt = (
            select(
                message_history_table.c.role,
                message_history_table.c.content,
                message_history_table.c.tool_calls_info,
            )
            .where(message_history_table.c.chat_id == chat_id)
            .where(message_history_table.c.timestamp >= cutoff_time)
            .order_by(message_history_table.c.timestamp.desc())
            .limit(limit)
        )
        rows = await db_context.fetch_all(stmt)
        formatted_rows = []
        for row in reversed(rows): # Reverse here to get chronological order
            msg = {"role": row["role"], "content": row["content"]}
            if row["role"] == "assistant" and row["tool_calls_info"]:
                msg["tool_calls_info_raw"] = row["tool_calls_info"]
            formatted_rows.append(msg)
        return formatted_rows
    except SQLAlchemyError as e:
        logger.error(
            f"Database error in get_recent_history({chat_id}): {e}",
            exc_info=True,
        )
        raise


async def get_message_by_id(
    db_context: DatabaseContext, # Added context
    chat_id: int,
    message_id: int
) -> Optional[Dict[str, Any]]:
    """Retrieves a specific message by its chat and message ID."""
    try:
        stmt = (
            select(message_history_table.c.role, message_history_table.c.content)
            .where(message_history_table.c.chat_id == chat_id)
            .where(message_history_table.c.message_id == message_id)
        )
        row = await db_context.fetch_one(stmt)
        return {"role": row["role"], "content": row["content"]} if row else None
    except SQLAlchemyError as e:
        logger.error(
            f"Database error in get_message_by_id({chat_id}, {message_id}): {e}",
            exc_info=True,
        )
        raise


async def get_grouped_message_history(
    db_context: DatabaseContext # Added context
) -> Dict[int, List[Dict[str, Any]]]:
    """Retrieves all message history, grouped by chat_id and ordered by timestamp."""
    try:
        stmt = select(message_history_table).order_by(  # Select all columns
            message_history_table.c.chat_id, message_history_table.c.timestamp.desc()
        )
        rows = await db_context.fetch_all(stmt)
        grouped_history = {}
        for row in rows:
            chat_id = row["chat_id"]
            if chat_id not in grouped_history:
                grouped_history[chat_id] = []
            # row is already a dict-like mapping from fetch_all
            grouped_history[chat_id].append(row)
        return grouped_history
    except SQLAlchemyError as e:
        logger.error(
            f"Database error in get_grouped_message_history: {e}",
            exc_info=True,
        )
        raise
