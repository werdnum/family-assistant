"""Integration tests for message history notification hook."""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from family_assistant.storage.base import metadata
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.web.message_notifier import MessageNotifier

# Use an in-memory SQLite database for unit tests
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture(scope="function")
async def db_engine() -> AsyncGenerator[AsyncEngine]:
    """Creates an in-memory SQLite engine and sets up the schema for each test function."""
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)

    yield engine

    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def message_notifier() -> MessageNotifier:
    """Provides a MessageNotifier instance for tests."""
    return MessageNotifier()


@pytest_asyncio.fixture(scope="function")
async def db_context(
    db_engine: AsyncEngine, message_notifier: MessageNotifier
) -> AsyncGenerator[DatabaseContext]:
    """Provides an *entered* DatabaseContext instance with MessageNotifier."""
    context_instance = get_db_context(
        engine=db_engine, base_delay=0.01, message_notifier=message_notifier
    )
    async with context_instance as entered_context:
        yield entered_context


@pytest.mark.asyncio
async def test_message_notifier_accessible(
    db_context: DatabaseContext, message_notifier: MessageNotifier
) -> None:
    """Test that message_notifier is accessible from DatabaseContext."""
    assert hasattr(db_context, "message_notifier")
    assert db_context.message_notifier is message_notifier


@pytest.mark.asyncio
async def test_add_message_notifies_listeners(
    db_engine: AsyncEngine, message_notifier: MessageNotifier
) -> None:
    """Test that add_message triggers notification to registered listeners."""
    interface_type = "web"
    conversation_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    # Register a listener before adding the message
    queue = await message_notifier.register(conversation_id, interface_type)

    # Create a context, add message, then exit (which triggers commit and notification)
    async with get_db_context(
        engine=db_engine, base_delay=0.01, message_notifier=message_notifier
    ) as db_context:
        msg = await db_context.message_history.add_message(
            interface_type=interface_type,
            conversation_id=conversation_id,
            interface_message_id="msg1",
            turn_id=None,
            thread_root_id=None,
            timestamp=now,
            role="user",
            content="Test message",
        )

        assert msg is not None

    # Now that we've exited the context (commit happened), check for notification
    assert queue.qsize() == 1, f"Expected queue size 1, got {queue.qsize()}"
    tickle = await queue.get()
    assert tickle is True


@pytest.mark.asyncio
async def test_add_message_without_notifier_works(db_engine: AsyncEngine) -> None:
    """Test that add_message works correctly when no notifier is provided."""
    # Create context WITHOUT notifier
    async with get_db_context(engine=db_engine, base_delay=0.01) as db_context:
        interface_type = "web"
        conversation_id = str(uuid.uuid4())
        now = datetime.now(UTC)

        # Add a message - should work fine without notifier
        msg = await db_context.message_history.add_message(
            interface_type=interface_type,
            conversation_id=conversation_id,
            interface_message_id="msg1",
            turn_id=None,
            thread_root_id=None,
            timestamp=now,
            role="user",
            content="Test message",
        )

        assert msg is not None
        assert msg["content"] == "Test message"


@pytest.mark.asyncio
async def test_multiple_listeners_receive_notifications(
    db_engine: AsyncEngine, message_notifier: MessageNotifier
) -> None:
    """Test that multiple listeners for the same conversation receive notifications."""
    interface_type = "web"
    conversation_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    # Register multiple listeners for the same conversation
    queue1 = await message_notifier.register(conversation_id, interface_type)
    queue2 = await message_notifier.register(conversation_id, interface_type)
    queue3 = await message_notifier.register(conversation_id, interface_type)

    # Add a message and commit
    async with get_db_context(
        engine=db_engine, base_delay=0.01, message_notifier=message_notifier
    ) as db_context:
        await db_context.message_history.add_message(
            interface_type=interface_type,
            conversation_id=conversation_id,
            interface_message_id="msg1",
            turn_id=None,
            thread_root_id=None,
            timestamp=now,
            role="user",
            content="Test message",
        )

    # All three queues should have received a tickle
    assert queue1.qsize() == 1
    assert queue2.qsize() == 1
    assert queue3.qsize() == 1

    assert await queue1.get() is True
    assert await queue2.get() is True
    assert await queue3.get() is True


@pytest.mark.asyncio
async def test_only_matching_conversation_notified(
    db_engine: AsyncEngine, message_notifier: MessageNotifier
) -> None:
    """Test that only listeners for the specific conversation receive notifications."""
    interface_type = "web"
    conv1 = str(uuid.uuid4())
    conv2 = str(uuid.uuid4())
    now = datetime.now(UTC)

    # Register listeners for different conversations
    queue1 = await message_notifier.register(conv1, interface_type)
    queue2 = await message_notifier.register(conv2, interface_type)

    # Add message to conv1 only and commit
    async with get_db_context(
        engine=db_engine, base_delay=0.01, message_notifier=message_notifier
    ) as db_context:
        await db_context.message_history.add_message(
            interface_type=interface_type,
            conversation_id=conv1,
            interface_message_id="msg1",
            turn_id=None,
            thread_root_id=None,
            timestamp=now,
            role="user",
            content="Test message",
        )

    # Only queue1 should have received notification
    assert queue1.qsize() == 1
    assert queue2.qsize() == 0

    assert await queue1.get() is True


@pytest.mark.asyncio
async def test_interface_type_isolation(
    db_engine: AsyncEngine, message_notifier: MessageNotifier
) -> None:
    """Test that notifications are scoped to specific interface types."""
    conversation_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    # Register listeners for different interface types
    web_queue = await message_notifier.register(conversation_id, "web")
    telegram_queue = await message_notifier.register(conversation_id, "telegram")

    # Add message to web interface and commit
    async with get_db_context(
        engine=db_engine, base_delay=0.01, message_notifier=message_notifier
    ) as db_context:
        await db_context.message_history.add_message(
            interface_type="web",
            conversation_id=conversation_id,
            interface_message_id="msg1",
            turn_id=None,
            thread_root_id=None,
            timestamp=now,
            role="user",
            content="Web message",
        )

    # Only web_queue should have received notification
    assert web_queue.qsize() == 1
    assert telegram_queue.qsize() == 0

    assert await web_queue.get() is True
