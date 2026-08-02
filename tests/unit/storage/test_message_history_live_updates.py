"""Unit tests for message history live updates functionality."""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm.messages import AssistantMessage, UserMessage
from family_assistant.storage.base import (
    create_engine_with_sqlite_optimizations,
    metadata,
)
from family_assistant.storage.database import Database
from tests.conftest import check_db_engine_invariants

# Use an in-memory SQLite database for unit tests
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture(scope="function")
async def db_engine() -> AsyncGenerator[AsyncEngine]:
    """Creates an in-memory SQLite engine and sets up the schema for each test function."""
    engine = create_engine_with_sqlite_optimizations(TEST_DATABASE_URL, instrument=True)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)

    yield engine

    await check_db_engine_invariants(
        engine, "test_message_history_live_updates db_engine"
    )
    await engine.dispose()


@pytest.fixture
def db_context(db_engine: AsyncEngine) -> Database:
    """Provides an *entered* Database instance for interacting with the test database."""
    return Database(engine=db_engine, base_delay=0.01)


@pytest.mark.asyncio
async def test_get_messages_after_basic_query(db_context: Database) -> None:
    """Test basic functionality of get_messages_after."""
    interface_type = "web"
    conversation_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    # Add messages at different timestamps
    msg1 = await db_context.message_history.add_message(
        UserMessage(content="Old message 1"),
        interface_type=interface_type,
        conversation_id=conversation_id,
        interface_message_id="msg1",
        turn_id=None,
        thread_root_id=None,
        timestamp=now - timedelta(minutes=10),
    )

    msg2 = await db_context.message_history.add_message(
        AssistantMessage(content="Old message 2"),
        interface_type=interface_type,
        conversation_id=conversation_id,
        interface_message_id="msg2",
        turn_id=None,
        thread_root_id=None,
        timestamp=now - timedelta(minutes=5),
    )

    msg3 = await db_context.message_history.add_message(
        UserMessage(content="Recent message 1"),
        interface_type=interface_type,
        conversation_id=conversation_id,
        interface_message_id="msg3",
        turn_id=None,
        thread_root_id=None,
        timestamp=now - timedelta(minutes=2),
    )

    msg4 = await db_context.message_history.add_message(
        AssistantMessage(content="Recent message 2"),
        interface_type=interface_type,
        conversation_id=conversation_id,
        interface_message_id="msg4",
        turn_id=None,
        thread_root_id=None,
        timestamp=now - timedelta(minutes=1),
    )

    assert msg1 is not None
    assert msg2 is not None
    assert msg3 is not None
    assert msg4 is not None

    # Query messages after 3 minutes ago - should get msg3 and msg4
    cutoff = now - timedelta(minutes=3)
    messages = await db_context.message_history.get_messages_after(
        conversation_id=conversation_id, after=cutoff
    )

    # Should return 2 messages
    assert len(messages) == 2
    assert messages[0].content == "Recent message 1"
    assert messages[1].content == "Recent message 2"


@pytest.mark.asyncio
async def test_get_messages_after_filter_by_interface_type(
    db_context: Database,
) -> None:
    """Test filtering by interface_type in get_messages_after."""
    conversation_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    # Add messages with different interface types
    msg1 = await db_context.message_history.add_message(
        UserMessage(content="Web message"),
        interface_type="web",
        conversation_id=conversation_id,
        interface_message_id="msg1",
        turn_id=None,
        thread_root_id=None,
        timestamp=now - timedelta(minutes=5),
    )

    msg2 = await db_context.message_history.add_message(
        UserMessage(content="Telegram message"),
        interface_type="telegram",
        conversation_id=conversation_id,
        interface_message_id="msg2",
        turn_id=None,
        thread_root_id=None,
        timestamp=now - timedelta(minutes=4),
    )

    msg3 = await db_context.message_history.add_message(
        AssistantMessage(content="Web response"),
        interface_type="web",
        conversation_id=conversation_id,
        interface_message_id="msg3",
        turn_id=None,
        thread_root_id=None,
        timestamp=now - timedelta(minutes=3),
    )

    assert msg1 is not None
    assert msg2 is not None
    assert msg3 is not None

    # Query messages after 6 minutes ago, filtered by "web"
    cutoff = now - timedelta(minutes=6)
    messages = await db_context.message_history.get_messages_after(
        conversation_id=conversation_id, after=cutoff, interface_type="web"
    )

    # Should only return web messages (msg1 and msg3)
    assert len(messages) == 2
    # Note: get_messages_after returns LLMMessage objects without interface_type metadata.
    # If we need to verify interface_type, we would need to use a method that returns
    # dict rows with metadata.


@pytest.mark.asyncio
async def test_get_messages_after_ordering_by_timestamp(
    db_context: Database,
) -> None:
    """Test that messages are ordered by timestamp ascending (oldest first)."""
    interface_type = "web"
    conversation_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    # Add messages in non-chronological order
    msg1 = await db_context.message_history.add_message(
        UserMessage(content="Second message"),
        interface_type=interface_type,
        conversation_id=conversation_id,
        interface_message_id="msg1",
        turn_id=None,
        thread_root_id=None,
        timestamp=now - timedelta(minutes=1),
    )

    msg2 = await db_context.message_history.add_message(
        UserMessage(content="First message"),
        interface_type=interface_type,
        conversation_id=conversation_id,
        interface_message_id="msg2",
        turn_id=None,
        thread_root_id=None,
        timestamp=now - timedelta(minutes=5),
    )

    msg3 = await db_context.message_history.add_message(
        UserMessage(content="Third message"),
        interface_type=interface_type,
        conversation_id=conversation_id,
        interface_message_id="msg3",
        turn_id=None,
        thread_root_id=None,
        timestamp=now,
    )

    assert msg1 is not None
    assert msg2 is not None
    assert msg3 is not None

    # Query all messages
    cutoff = now - timedelta(minutes=10)
    messages = await db_context.message_history.get_messages_after(
        conversation_id=conversation_id, after=cutoff
    )

    # Should be ordered chronologically (oldest first)
    assert len(messages) == 3
    assert messages[0].content == "First message"
    assert messages[1].content == "Second message"
    assert messages[2].content == "Third message"


@pytest.mark.asyncio
async def test_get_messages_after_limit_parameter(db_context: Database) -> None:
    """Test that the limit parameter restricts the number of results."""
    interface_type = "web"
    conversation_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    # Add 5 messages
    for i in range(5):
        await db_context.message_history.add_message(
            UserMessage(content=f"Message {i}"),
            interface_type=interface_type,
            conversation_id=conversation_id,
            interface_message_id=f"msg{i}",
            turn_id=None,
            thread_root_id=None,
            timestamp=now - timedelta(minutes=5 - i),
        )

    # Query with limit of 3
    cutoff = now - timedelta(minutes=10)
    messages = await db_context.message_history.get_messages_after(
        conversation_id=conversation_id, after=cutoff, limit=3
    )

    # Should return only 3 messages (the oldest 3)
    assert len(messages) == 3
    assert messages[0].content == "Message 0"
    assert messages[1].content == "Message 1"
    assert messages[2].content == "Message 2"


@pytest.mark.asyncio
async def test_get_messages_after_empty_results(db_context: Database) -> None:
    """Test that empty list is returned when no messages exist after timestamp."""
    interface_type = "web"
    conversation_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    # Add a message in the past
    await db_context.message_history.add_message(
        UserMessage(content="Old message"),
        interface_type=interface_type,
        conversation_id=conversation_id,
        interface_message_id="msg1",
        turn_id=None,
        thread_root_id=None,
        timestamp=now - timedelta(minutes=10),
    )

    # Query for messages after "now" - should be empty
    cutoff = now
    messages = await db_context.message_history.get_messages_after(
        conversation_id=conversation_id, after=cutoff
    )

    assert len(messages) == 0


@pytest.mark.asyncio
async def test_get_messages_after_different_conversations(
    db_context: Database,
) -> None:
    """Test that messages from different conversations are not mixed."""
    interface_type = "web"
    conv1 = str(uuid.uuid4())
    conv2 = str(uuid.uuid4())
    now = datetime.now(UTC)

    # Add messages to conversation 1
    msg1 = await db_context.message_history.add_message(
        UserMessage(content="Conv 1 message"),
        interface_type=interface_type,
        conversation_id=conv1,
        interface_message_id="msg1",
        turn_id=None,
        thread_root_id=None,
        timestamp=now - timedelta(minutes=5),
    )

    # Add messages to conversation 2
    await db_context.message_history.add_message(
        UserMessage(content="Conv 2 message"),
        interface_type=interface_type,
        conversation_id=conv2,
        interface_message_id="msg2",
        turn_id=None,
        thread_root_id=None,
        timestamp=now - timedelta(minutes=4),
    )

    assert msg1 is not None

    # Query conversation 1 only
    cutoff = now - timedelta(minutes=10)
    messages = await db_context.message_history.get_messages_after(
        conversation_id=conv1, after=cutoff
    )

    # Should only return messages from conv1
    assert len(messages) == 1
    assert messages[0].content == "Conv 1 message"
