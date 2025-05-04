"""Functional tests for message history storage operations."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator, Dict, List, Optional

import pytest
import pytest_asyncio # Need this for async fixtures
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine # Need these for engine fixture

from family_assistant.storage.context import DatabaseContext, get_db_context # Need get_db_context for fixture
from family_assistant.storage.message_history import (
    add_message_to_history,
    get_message_by_interface_id,
    get_messages_by_thread_id,
    get_messages_by_turn_id,
    get_recent_history,
    update_message_interface_id,
)

# Import metadata to create tables
from family_assistant.storage.base import metadata

# Use an in-memory SQLite database for functional storage tests
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture(scope="function")
async def db_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Creates an in-memory SQLite engine and sets up the schema for each test function."""
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        # Ensure tables are created - only creates if they don't exist
        await conn.run_sync(metadata.create_all)

    yield engine

    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def db_context(db_engine: AsyncEngine) -> AsyncGenerator[DatabaseContext, None]:
    """Provides an *entered* DatabaseContext instance for interacting with the test database."""
    # Using the factory function aligns better with potential future DI usage
    context_instance = await get_db_context(engine=db_engine, base_delay=0.01)
    async with context_instance as entered_context:
        yield entered_context
    # Context is automatically exited here


@pytest.mark.asyncio
async def test_add_message_stores_optional_fields(db_context: DatabaseContext):
    """Verify storing messages with optional fields populated."""
    # Arrange
    interface_type = "test_optional" # Define interface_type
    conversation_id = str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    thread_root_id = 123  # Assume this ID exists from a previous message
    now = datetime.now(timezone.utc)
    role = "assistant"
    tool_calls_data = [{"id": "call_abc", "type": "function", "function": {"name": "get_weather", "arguments": '{"location": "London"}'}}]
    reasoning_data = {"model": "test-model", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    error_trace = "Something went wrong"
    tool_call_id = "call_abc" # For a potential 'tool' role message

    # Act: Store messages using the yielded, entered context
    # Store an assistant message with tool calls and reasoning
    assistant_msg_result = await add_message_to_history(
        db_context=db_context, # Use the yielded context directly
        interface_type=interface_type,
        conversation_id=conversation_id,
        interface_message_id=None, # Assistant msg might not have one initially
        turn_id=turn_id,
        thread_root_id=thread_root_id,
        timestamp=now,
    role=role,
    content="Calling tool...",
    tool_calls=tool_calls_data,
    reasoning_info=reasoning_data,
)
    # Store a tool response message
    tool_msg_result = await add_message_to_history( # Renamed variable to avoid confusion
        db_context=db_context, # Use the yielded context directly
            interface_type=interface_type,
            conversation_id=conversation_id,
            interface_message_id=None,
            turn_id=turn_id,
            thread_root_id=thread_root_id,
            timestamp=now + timedelta(milliseconds=100),
    role="tool",
    content="Weather is sunny",
    tool_call_id=tool_call_id,
    error_traceback=error_trace, # Can store traceback even for non-error roles if needed
)

    assert assistant_msg_result is not None and assistant_msg_result.get("internal_id") is not None
    assistant_msg_internal_id = assistant_msg_result["internal_id"]
    assert tool_msg_result is not None and tool_msg_result.get("internal_id") is not None
    tool_msg_internal_id = tool_msg_result["internal_id"]

    # Assert Assistant Message
    # Use the yielded db_context directly
    assistant_result = await db_context.fetch_one(
        text("SELECT * FROM message_history WHERE internal_id = :id"),
        {"id": assistant_msg_internal_id}, # Use the correct variable name
    )
    assert assistant_result is not None
    assert assistant_result["turn_id"] == turn_id
    assert assistant_result["thread_root_id"] == thread_root_id
    assert assistant_result["tool_calls"] == tool_calls_data # Check JSON storage
    assert assistant_result["reasoning_info"] == reasoning_data
    assert assistant_result["tool_call_id"] is None # Assistant doesn't have tool_call_id
    assert assistant_result["error_traceback"] is None

    # Assert Tool Message
    # Use the yielded db_context directly
    tool_result = await db_context.fetch_one(
        text("SELECT * FROM message_history WHERE internal_id = :id"),
        {"id": tool_msg_internal_id}, # Use the correct variable name
    )
    assert tool_result is not None
    assert tool_result["turn_id"] == turn_id
    assert tool_result["thread_root_id"] == thread_root_id
    assert tool_result["tool_call_id"] == tool_call_id
    assert tool_result["error_traceback"] == error_trace
    assert tool_result["tool_calls"] is None
    assert tool_result["reasoning_info"] is None


@pytest.mark.asyncio
async def test_get_recent_history_retrieves_correct_messages(db_context: DatabaseContext):
    """Verify get_recent_history filters, limits, orders, and handles age correctly."""
    # Arrange
    interface = "history_test"
    conv_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    # Add messages using the yielded context
    msg1_id_result = await add_message_to_history(db_context, interface_type=interface, conversation_id=conv_id, interface_message_id="msg1", turn_id=None, thread_root_id=None, timestamp=now - timedelta(minutes=10), role="user", content="Old message")
    msg2_id_result = await add_message_to_history(db_context, interface_type=interface, conversation_id=conv_id, interface_message_id="msg2", turn_id=None, thread_root_id=None, timestamp=now - timedelta(minutes=2), role="assistant", content="Recent 1")
    msg3_id_result = await add_message_to_history(db_context, interface_type=interface, conversation_id=conv_id, interface_message_id="msg3", turn_id=None, thread_root_id=None, timestamp=now - timedelta(minutes=1), role="user", content="Recent 2")
    # Add a message for a different conversation
    await add_message_to_history(db_context, interface_type=interface, conversation_id="other_conv", interface_message_id="msg_other", turn_id=None, thread_root_id=None, timestamp=now, role="user", content="Other convo")

        # Act: Get recent history with limit and age cutoff
    recent_messages = await get_recent_history(
        db_context, # Use the yielded context
        interface_type=interface,
        conversation_id=conv_id,
            limit=2,
            max_age=timedelta(minutes=5) # Should exclude msg1
        )

    # Assert
    assert len(recent_messages) == 2 # Limit respected
    assert msg1_id_result is not None and msg1_id_result.get("internal_id") is not None
    msg1_internal_id = msg1_id_result["internal_id"]
    assert msg2_id_result is not None and msg2_id_result.get("internal_id") is not None
    msg2_internal_id = msg2_id_result["internal_id"]
    assert msg3_id_result is not None and msg3_id_result.get("internal_id") is not None
    msg3_internal_id = msg3_id_result["internal_id"]
    # Check chronological order (oldest first in the returned list)
    assert recent_messages[0]["internal_id"] == msg2_id_result["internal_id"]
    assert recent_messages[1]["internal_id"] == msg3_id_result["internal_id"]
    assert recent_messages[0]["content"] == "Recent 1"
    assert recent_messages[1]["content"] == "Recent 2"
    # Verify msg1 (too old) and msg_other (different convo) are not included
    assert all(msg["internal_id"] != msg1_id_result["internal_id"] for msg in recent_messages)


@pytest.mark.asyncio
async def test_get_message_by_interface_id_retrieval(db_context: DatabaseContext):
    """Verify retrieving a specific message by its interface identifiers."""
    # Arrange
    interface = "get_by_id"
    conv_id = str(uuid.uuid4())
    msg_id = "message_abc"
    now = datetime.now(timezone.utc)
    content = "Target message"

    internal_id_result = await add_message_to_history(db_context, interface_type=interface, conversation_id=conv_id, interface_message_id=msg_id, turn_id=None, thread_root_id=None, timestamp=now, role="user", content=content)
    assert internal_id_result is not None and internal_id_result.get("internal_id") is not None
    msg_internal_id = internal_id_result["internal_id"]

        # Act: Retrieve the message
    retrieved_message = await get_message_by_interface_id(db_context, interface, conv_id, msg_id)

    assert retrieved_message is not None
    assert retrieved_message["internal_id"] == internal_id_result["internal_id"]
    assert retrieved_message["interface_type"] == interface
    assert retrieved_message["conversation_id"] == conv_id
    assert retrieved_message["interface_message_id"] == msg_id
    assert retrieved_message["content"] == content

    # Act: Try to retrieve non-existent message (needs context)
    not_found_message = await get_message_by_interface_id(db_context, interface, conv_id, "non_existent_id")

    # Assert
    assert not_found_message is None


@pytest.mark.asyncio
async def test_get_messages_by_turn_id_retrieves_correct_sequence(db_context: DatabaseContext):
    """Verify retrieving all messages for a specific turn_id in order."""
     # Arrange
    interface = "turn_test"
    conv_id = str(uuid.uuid4())
    turn_1 = str(uuid.uuid4())
    turn_2 = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    # Turn 1 messages
    t1_msg1_result = await add_message_to_history(db_context, interface_type=interface, conversation_id=conv_id, interface_message_id=None, turn_id=turn_1, thread_root_id=1, timestamp=now, role="assistant", content="T1 Call tool")
    t1_msg2_result = await add_message_to_history(db_context, interface_type=interface, conversation_id=conv_id, interface_message_id=None, turn_id=turn_1, thread_root_id=1, timestamp=now + timedelta(seconds=1), role="tool", content="T1 Tool result")
    t1_msg3_result = await add_message_to_history(db_context, interface_type=interface, conversation_id=conv_id, interface_message_id=None, turn_id=turn_1, thread_root_id=1, timestamp=now + timedelta(seconds=2), role="assistant", content="T1 Final answer")
    # Turn 2 message
    t2_msg1_result = await add_message_to_history(db_context, interface_type=interface, conversation_id=conv_id, interface_message_id=None, turn_id=turn_2, thread_root_id=1, timestamp=now + timedelta(seconds=3), role="assistant", content="T2 Different turn")
    # Message with no turn id
    no_turn_msg_result = await add_message_to_history(db_context, interface_type=interface, conversation_id=conv_id, interface_message_id="user1", turn_id=None, thread_root_id=1, timestamp=now - timedelta(seconds=1), role="user", content="Initial prompt")
    # Assert that results contain IDs
    assert t1_msg1_result is not None and t1_msg1_result.get("internal_id") is not None
    t1_msg1_internal_id = t1_msg1_result["internal_id"]
    assert t1_msg2_result is not None and t1_msg2_result.get("internal_id") is not None
    t1_msg2_internal_id = t1_msg2_result["internal_id"]
    assert t1_msg3_result is not None and t1_msg3_result.get("internal_id") is not None
    t1_msg3_internal_id = t1_msg3_result["internal_id"]

    # Act
    turn_1_messages = await get_messages_by_turn_id(db_context, turn_1)

    # Assert
    assert len(turn_1_messages) == 3
    assert [m["internal_id"] for m in turn_1_messages] == [t1_msg1_result["internal_id"], t1_msg2_result["internal_id"], t1_msg3_result["internal_id"]] # Check order
    assert all(m["turn_id"] == turn_1 for m in turn_1_messages)

    # Act: Get messages for a turn with no messages (needs context)
    empty_turn_messages = await get_messages_by_turn_id(db_context, str(uuid.uuid4()))
    # Assert
    assert len(empty_turn_messages) == 0


@pytest.mark.asyncio
async def test_update_message_interface_id_sets_id(db_context: DatabaseContext):
    """Verify that the interface message ID can be updated after insertion."""
    # Arrange
    interface = "update_test"
    conv_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    new_interface_id = f"telegram_{uuid.uuid4()}"

    initial_result = await add_message_to_history(
        db_context, interface, conv_id, None, str(uuid.uuid4()), 1, now, "assistant", "Initial content"
    )
    assert initial_result is not None and initial_result.get("internal_id") is not None
    internal_id = initial_result["internal_id"]

    # Act
    update_successful = await update_message_interface_id(db_context, internal_id, new_interface_id)

    # Assert
    assert update_successful is True
    # Verify directly in the DB
    result = await db_context.fetch_one(
            text("SELECT interface_message_id FROM message_history WHERE internal_id = :id"),
            {"id": internal_id}, # internal_id is the integer ID itself here
        )
    assert result is not None
    assert result["interface_message_id"] == new_interface_id

    # Act: Try to update non-existent internal ID (needs context)
    update_failed = await update_message_interface_id(db_context, 99999, "some_id")
    # Assert
    assert update_failed is False


@pytest.mark.asyncio
async def test_get_messages_by_thread_id_retrieves_correct_sequence(db_context: DatabaseContext):
    """Verify retrieving all messages for a specific thread_root_id in order."""
    # Arrange
    interface = "thread_test"
    conv_id_1 = str(uuid.uuid4())
    conv_id_2 = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

        # Thread 1 messages
    msg1_result = await add_message_to_history(db_context, interface_type=interface, conversation_id=conv_id_1, interface_message_id="msg1", turn_id=None, thread_root_id=None, timestamp=now, role="user", content="Thread 1 Start")
    assert msg1_result is not None and msg1_result.get("internal_id") is not None
    thread_1_root = msg1_result["internal_id"] # Use the internal_id of the first message as the root
    msg1_id = thread_1_root # Keep for assertion later

    msg2_result = await add_message_to_history(db_context, interface_type=interface, conversation_id=conv_id_1, interface_message_id=None, turn_id="t1", thread_root_id=thread_1_root, timestamp=now + timedelta(seconds=1), role="assistant", content="Thread 1 Reply 1")
    assert msg2_result is not None and msg2_result.get("internal_id") is not None
    msg2_id = msg2_result["internal_id"]

    msg3_result = await add_message_to_history(db_context, interface_type=interface, conversation_id=conv_id_1, interface_message_id="msg3", turn_id=None, thread_root_id=thread_1_root, timestamp=now + timedelta(seconds=2), role="user", content="Thread 1 Reply 2")
    assert msg3_result is not None and msg3_result.get("internal_id") is not None
    msg3_id = msg3_result["internal_id"]

    # Thread 2 message (Different conversation, different thread)
    msg4_result = await add_message_to_history(db_context, interface_type=interface, conversation_id=conv_id_2, interface_message_id="msg4", turn_id=None, thread_root_id=None, timestamp=now + timedelta(seconds=3), role="user", content="Thread 2 Start")
    assert msg4_result is not None and msg4_result.get("internal_id") is not None
    msg4_id = msg4_result["internal_id"]

    # Act
    thread_1_messages = await get_messages_by_thread_id(db_context, thread_1_root)

    # Assert
    assert len(thread_1_messages) == 3
    assert [m["internal_id"] for m in thread_1_messages] == [msg1_id, msg2_id, msg3_id] # Check order
    assert all(m["thread_root_id"] == thread_1_root or m["internal_id"] == thread_1_root for m in thread_1_messages) # Root msg has NULL thread_root_id

    # Act: Get messages for a thread_root_id that doesn't exist (use msg4_id)
    empty_thread_messages = await get_messages_by_thread_id(db_context, msg4_id)
    # Assert outside context
    assert len(empty_thread_messages) == 0
