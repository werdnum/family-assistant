"""
Handles storage and retrieval of message history.
"""

import logging
import random
import asyncio
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional

from sqlalchemy import (
    Table, Column, String, BigInteger, DateTime, Text, JSON, select, insert, desc
)
from sqlalchemy.exc import DBAPIError

from db_base import metadata, get_engine

logger = logging.getLogger(__name__)
engine = get_engine()

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
    chat_id: int,
    message_id: int,
    timestamp: datetime,
    role: str,
    content: str,
    tool_calls_info: Optional[List[Dict[str, Any]]] = None,
):
    """Adds a message to the history table, including optional tool call info, with retries."""
    max_retries = 3
    base_delay = 0.5
    stmt = insert(message_history_table).values(
        chat_id=chat_id, message_id=message_id, timestamp=timestamp, role=role, content=content, tool_calls_info=tool_calls_info
    )

    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                await conn.execute(stmt)
                await conn.commit()
                logger.debug(f"Added message {message_id} from chat {chat_id} to history.")
                return
        except DBAPIError as e:
            logger.warning(f"DBAPIError in add_message_to_history (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error(f"Max retries exceeded for add_message_to_history({chat_id}, {message_id}). Raising error.")
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Non-retryable error in add_message_to_history({chat_id}, {message_id}): {e}", exc_info=True)
            raise
    raise RuntimeError(f"Database operation failed for add_message_to_history({chat_id}, {message_id}) after multiple retries")


async def get_recent_history(chat_id: int, limit: int, max_age: timedelta) -> List[Dict[str, Any]]:
    """Retrieves recent messages for a chat, including tool call info, with retries."""
    cutoff_time = datetime.now(timezone.utc) - max_age
    max_retries = 3
    base_delay = 0.5
    stmt = (
        select(message_history_table.c.role, message_history_table.c.content, message_history_table.c.tool_calls_info)
        .where(message_history_table.c.chat_id == chat_id)
        .where(message_history_table.c.timestamp >= cutoff_time)
        .order_by(message_history_table.c.timestamp.desc())
        .limit(limit)
    )

    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                result = await conn.execute(stmt)
                rows = result.fetchall()
                formatted_rows = []
                for row in reversed(rows):
                    msg = {"role": row.role, "content": row.content}
                    if row.role == "assistant" and row.tool_calls_info:
                        msg["tool_calls_info_raw"] = row.tool_calls_info
                    formatted_rows.append(msg)
                return formatted_rows
        except DBAPIError as e:
            logger.warning(f"DBAPIError in get_recent_history (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error(f"Max retries exceeded for get_recent_history({chat_id}). Raising error.")
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Non-retryable error in get_recent_history({chat_id}): {e}", exc_info=True)
            raise
    raise RuntimeError(f"Database operation failed for get_recent_history({chat_id}) after multiple retries")


async def get_message_by_id(chat_id: int, message_id: int) -> Optional[Dict[str, Any]]:
    """Retrieves a specific message by its chat and message ID, with retries."""
    max_retries = 3
    base_delay = 0.5
    stmt = (select(message_history_table.c.role, message_history_table.c.content)
            .where(message_history_table.c.chat_id == chat_id)
            .where(message_history_table.c.message_id == message_id))

    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                result = await conn.execute(stmt)
                row = result.fetchone()
                return {"role": row.role, "content": row.content} if row else None
        except DBAPIError as e:
            logger.warning(f"DBAPIError in get_message_by_id (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error(f"Max retries exceeded for get_message_by_id({chat_id}, {message_id}). Raising error.")
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Non-retryable error in get_message_by_id({chat_id}, {message_id}): {e}", exc_info=True)
            raise
    raise RuntimeError(f"Database operation failed for get_message_by_id({chat_id}, {message_id}) after multiple retries")


async def get_grouped_message_history() -> Dict[int, List[Dict[str, Any]]]:
    """Retrieves all message history, grouped by chat_id and ordered by timestamp."""
    max_retries = 3
    base_delay = 0.5
    stmt = (
        select(message_history_table) # Select all columns
        .order_by(message_history_table.c.chat_id, message_history_table.c.timestamp.desc())
    )

    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                result = await conn.execute(stmt)
                rows = result.fetchall()
                grouped_history = {}
                for row in rows:
                    chat_id = row.chat_id
                    if chat_id not in grouped_history:
                        grouped_history[chat_id] = []
                    # Convert row to dict for easier handling
                    grouped_history[chat_id].append(row._mapping)
                return grouped_history
        except DBAPIError as e:
            logger.warning(f"DBAPIError in get_grouped_message_history (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error("Max retries exceeded for get_grouped_message_history. Raising error.")
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Non-retryable error in get_grouped_message_history: {e}", exc_info=True)
            raise
    raise RuntimeError("Database operation failed for get_grouped_message_history after multiple retries")

"""
Handles storage and retrieval of message history.
"""

import logging
import random
import asyncio
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional

from sqlalchemy import (
    Table, Column, String, BigInteger, DateTime, Text, JSON, select, insert, desc
)
from sqlalchemy.exc import DBAPIError

from db_base import metadata, get_engine

logger = logging.getLogger(__name__)
engine = get_engine()

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
    chat_id: int,
    message_id: int,
    timestamp: datetime,
    role: str,
    content: str,
    tool_calls_info: Optional[List[Dict[str, Any]]] = None,
):
    """Adds a message to the history table, including optional tool call info, with retries."""
    max_retries = 3
    base_delay = 0.5
    stmt = insert(message_history_table).values(
        chat_id=chat_id, message_id=message_id, timestamp=timestamp, role=role, content=content, tool_calls_info=tool_calls_info
    )

    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                await conn.execute(stmt)
                await conn.commit()
                logger.debug(f"Added message {message_id} from chat {chat_id} to history.")
                return
        except DBAPIError as e:
            logger.warning(f"DBAPIError in add_message_to_history (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error(f"Max retries exceeded for add_message_to_history({chat_id}, {message_id}). Raising error.")
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Non-retryable error in add_message_to_history({chat_id}, {message_id}): {e}", exc_info=True)
            raise
    raise RuntimeError(f"Database operation failed for add_message_to_history({chat_id}, {message_id}) after multiple retries")


async def get_recent_history(chat_id: int, limit: int, max_age: timedelta) -> List[Dict[str, Any]]:
    """Retrieves recent messages for a chat, including tool call info, with retries."""
    cutoff_time = datetime.now(timezone.utc) - max_age
    max_retries = 3
    base_delay = 0.5
    stmt = (
        select(message_history_table.c.role, message_history_table.c.content, message_history_table.c.tool_calls_info)
        .where(message_history_table.c.chat_id == chat_id)
        .where(message_history_table.c.timestamp >= cutoff_time)
        .order_by(message_history_table.c.timestamp.desc())
        .limit(limit)
    )

    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                result = await conn.execute(stmt)
                rows = result.fetchall()
                formatted_rows = []
                for row in reversed(rows):
                    msg = {"role": row.role, "content": row.content}
                    if row.role == "assistant" and row.tool_calls_info:
                        msg["tool_calls_info_raw"] = row.tool_calls_info
                    formatted_rows.append(msg)
                return formatted_rows
        except DBAPIError as e:
            logger.warning(f"DBAPIError in get_recent_history (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error(f"Max retries exceeded for get_recent_history({chat_id}). Raising error.")
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Non-retryable error in get_recent_history({chat_id}): {e}", exc_info=True)
            raise
    raise RuntimeError(f"Database operation failed for get_recent_history({chat_id}) after multiple retries")


async def get_message_by_id(chat_id: int, message_id: int) -> Optional[Dict[str, Any]]:
    """Retrieves a specific message by its chat and message ID, with retries."""
    max_retries = 3
    base_delay = 0.5
    stmt = (select(message_history_table.c.role, message_history_table.c.content)
            .where(message_history_table.c.chat_id == chat_id)
            .where(message_history_table.c.message_id == message_id))

    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                result = await conn.execute(stmt)
                row = result.fetchone()
                return {"role": row.role, "content": row.content} if row else None
        except DBAPIError as e:
            logger.warning(f"DBAPIError in get_message_by_id (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error(f"Max retries exceeded for get_message_by_id({chat_id}, {message_id}). Raising error.")
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Non-retryable error in get_message_by_id({chat_id}, {message_id}): {e}", exc_info=True)
            raise
    raise RuntimeError(f"Database operation failed for get_message_by_id({chat_id}, {message_id}) after multiple retries")


async def get_grouped_message_history() -> Dict[int, List[Dict[str, Any]]]:
    """Retrieves all message history, grouped by chat_id and ordered by timestamp."""
    max_retries = 3
    base_delay = 0.5
    stmt = (
        select(message_history_table) # Select all columns
        .order_by(message_history_table.c.chat_id, message_history_table.c.timestamp.desc())
    )

    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                result = await conn.execute(stmt)
                rows = result.fetchall()
                grouped_history = {}
                for row in rows:
                    chat_id = row.chat_id
                    if chat_id not in grouped_history:
                        grouped_history[chat_id] = []
                    # Convert row to dict for easier handling
                    grouped_history[chat_id].append(row._mapping)
                return grouped_history
        except DBAPIError as e:
            logger.warning(f"DBAPIError in get_grouped_message_history (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error("Max retries exceeded for get_grouped_message_history. Raising error.")
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Non-retryable error in get_grouped_message_history: {e}", exc_info=True)
            raise
    raise RuntimeError("Database operation failed for get_grouped_message_history after multiple retries")

