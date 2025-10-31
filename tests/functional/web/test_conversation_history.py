"""Test conversation history endpoints for the chat API."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.assistant import Assistant
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.storage.context import get_db_context
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient


@pytest.mark.asyncio
async def test_get_conversations_empty(web_only_assistant: Assistant) -> None:
    """Test getting conversations when none exist."""
    # Create HTTP client using the assistant's owned fastapi app
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/chat/conversations")

        assert response.status_code == 200
        data = response.json()
        assert data["conversations"] == []
        assert data["count"] == 0


@pytest.mark.asyncio
async def test_get_conversations_with_data(
    web_only_assistant: Assistant, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test getting conversations with existing conversation data."""
    # Configure mock LLM to respond appropriately

    # Set up dynamic rules based on conversation content
    def matches_conv(i: int) -> Callable[[dict], bool]:
        def matcher(kwargs: dict) -> bool:
            messages = kwargs.get("messages", [])
            if messages:
                last_message = messages[-1].get("content", "")
                return f"conversation {i}" in last_message
            return False

        return matcher

    mock_llm_client.rules = [
        (matches_conv(0), LLMOutput(content="Hello! This is response 0")),
        (matches_conv(1), LLMOutput(content="Hello! This is response 1")),
        (matches_conv(2), LLMOutput(content="Hello! This is response 2")),
    ]

    # Create test conversations via API
    conv_ids = [f"test_conv_{i}" for i in range(3)]

    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Create conversations via chat API
        for i, conv_id in enumerate(conv_ids):
            response = await client.post(
                "/api/v1/chat/send_message",
                json={
                    "prompt": f"Hello from conversation {i}",
                    "conversation_id": conv_id,
                    "interface_type": "web",
                },
            )
            assert response.status_code == 200
            data = response.json()
            assert data["reply"] == f"Hello! This is response {i}"

        # Now get conversations via API
        response = await client.get("/api/v1/chat/conversations")

        assert response.status_code == 200
        data = response.json()
        assert len(data["conversations"]) == 3
        assert data["count"] == 3

        # Check conversation summaries
        for conv in data["conversations"]:
            assert conv["conversation_id"] in conv_ids
            assert conv["last_message"].startswith("Hello! This is response")
            assert conv["message_count"] == 2
            assert "last_timestamp" in conv


@pytest.mark.asyncio
async def test_get_conversations_pagination(
    web_only_assistant: Assistant, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test pagination of conversations list."""
    # Configure mock LLM

    # Simple response for all messages
    mock_llm_client.rules = [
        (lambda kwargs: True, LLMOutput(content="Test response")),
    ]

    # Create 5 test conversations via API
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        for i in range(5):
            conv_id = f"test_conv_page_{i}"
            response = await client.post(
                "/api/v1/chat/send_message",
                json={
                    "prompt": f"Message {i}",
                    "conversation_id": conv_id,
                    "interface_type": "web",
                },
            )
            assert response.status_code == 200
        # Get first page (limit 2)
        response = await client.get("/api/v1/chat/conversations?limit=2&offset=0")
        assert response.status_code == 200
        data = response.json()
        assert len(data["conversations"]) == 2
        assert data["count"] == 5

        # Get second page
        response = await client.get("/api/v1/chat/conversations?limit=2&offset=2")
        assert response.status_code == 200
        data = response.json()
        assert len(data["conversations"]) == 2
        assert data["count"] == 5

        # Get third page
        response = await client.get("/api/v1/chat/conversations?limit=2&offset=4")
        assert response.status_code == 200
        data = response.json()
        assert len(data["conversations"]) == 1
        assert data["count"] == 5


@pytest.mark.asyncio
async def test_get_conversation_messages_empty(web_only_assistant: Assistant) -> None:
    """Test getting messages for a non-existent conversation."""
    conv_id = "non_existent_conv"

    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(f"/api/v1/chat/conversations/{conv_id}/messages")

        assert response.status_code == 200
        data = response.json()
        assert data["conversation_id"] == conv_id
        assert data["messages"] == []
        assert data["count"] == 0
        assert data["total_messages"] == 0


@pytest.mark.asyncio
async def test_get_conversation_messages_with_data(
    web_only_assistant: Assistant, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test getting messages for an existing conversation."""

    conv_id = "test_conv_messages"

    # Configure mock LLM to simulate a tool call flow
    call_count = 0
    tool_call_id = "call_1"

    def weather_flow(kwargs: dict) -> LLMOutput:
        nonlocal call_count
        call_count += 1

        if call_count == 1:
            # First call: respond with tool call
            return LLMOutput(
                content="I'll check the weather for you.",
                tool_calls=[
                    ToolCallItem(
                        id=tool_call_id,
                        type="function",
                        function=ToolCallFunction(
                            name="get_weather",
                            arguments=json.dumps({"location": "current"}),
                        ),
                    )
                ],
            )
        else:
            # Second call: respond with final answer
            return LLMOutput(content="The weather is sunny and 72°F.")

    mock_llm_client.rules = [
        (lambda kwargs: True, weather_flow),
    ]

    # Create conversation via API
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Send the initial message
        response = await client.post(
            "/api/v1/chat/send_message",
            json={
                "prompt": "What is the weather like?",
                "conversation_id": conv_id,
                "interface_type": "web",
            },
        )
        assert response.status_code == 200
        assert "weather is sunny and 72°F" in response.json()["reply"]

        # Get conversation messages via API
        response = await client.get(f"/api/v1/chat/conversations/{conv_id}/messages")

        assert response.status_code == 200
        data = response.json()
        assert data["conversation_id"] == conv_id
        # Should have at least 4 messages: user, assistant with tool call, tool response, final assistant
        assert len(data["messages"]) >= 4
        assert data["count"] >= 4

        # Verify message roles and content
        messages = data["messages"]

        # Find messages by role
        user_msgs = [m for m in messages if m["role"] == "user"]
        assistant_msgs = [
            m for m in messages if m["role"] == "assistant" and not m.get("tool_calls")
        ]
        tool_call_msgs = [
            m for m in messages if m["role"] == "assistant" and m.get("tool_calls")
        ]
        tool_msgs = [m for m in messages if m["role"] == "tool"]

        # Verify we have the expected message types
        assert len(user_msgs) >= 1
        assert len(assistant_msgs) >= 1
        assert len(tool_call_msgs) >= 1
        assert len(tool_msgs) >= 1

        # Check content
        assert any("What is the weather like?" in m["content"] for m in user_msgs)
        assert any("weather is sunny and 72°F" in m["content"] for m in assistant_msgs)
        assert any("get_weather" in str(m["tool_calls"]) for m in tool_call_msgs)


@pytest.mark.asyncio
async def test_get_conversation_messages_cross_interface_retrieval(
    web_only_assistant: Assistant,
    db_engine: AsyncEngine,
) -> None:
    """Test that messages are retrieved from all interfaces for the same conversation ID."""
    conv_id = "test_conv_interface_filter"

    # Create test messages in different interfaces for same conversation ID
    async with get_db_context(db_engine) as db_context:
        # Add web message
        await db_context.message_history.add_message(
            interface_type="web",
            conversation_id=conv_id,
            interface_message_id="web_msg",
            turn_id="turn_1",
            thread_root_id=None,
            timestamp=datetime.now(UTC),
            role="user",
            content="Web message",
        )

        # Add telegram message to same conversation (should now appear)
        await db_context.message_history.add_message(
            interface_type="telegram",
            conversation_id=conv_id,
            interface_message_id="tg_msg",
            turn_id="turn_2",
            thread_root_id=None,
            timestamp=datetime.now(UTC),
            role="user",
            content="Telegram message",
        )

    # Get messages via API
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(f"/api/v1/chat/conversations/{conv_id}/messages")

        assert response.status_code == 200
        data = response.json()
        # Should now return messages from both interfaces
        assert len(data["messages"]) == 2
        assert data["count"] == 2
        assert data["total_messages"] == 2

        # Check that we have messages from both interfaces
        contents = [msg["content"] for msg in data["messages"]]
        assert "Web message" in contents
        assert "Telegram message" in contents


@pytest.mark.asyncio
async def test_get_conversations_interface_filter(
    web_only_assistant: Assistant,
    db_engine: AsyncEngine,
) -> None:
    """Test filtering conversations by interface type."""
    # Create test conversations in different interfaces
    async with get_db_context(db_engine) as db_context:
        # Add web conversation
        await db_context.message_history.add_message(
            interface_type="web",
            conversation_id="web_conv_filter_test",
            interface_message_id="web_msg_1",
            turn_id="turn_1",
            thread_root_id=None,
            timestamp=datetime.now(UTC),
            role="user",
            content="Web user message",
        )
        await db_context.message_history.add_message(
            interface_type="web",
            conversation_id="web_conv_filter_test",
            interface_message_id="web_msg_2",
            turn_id="turn_1",
            thread_root_id=None,
            timestamp=datetime.now(UTC),
            role="assistant",
            content="Web assistant response",
        )

        # Add telegram conversation
        await db_context.message_history.add_message(
            interface_type="telegram",
            conversation_id="tg_conv_filter_test",
            interface_message_id="tg_msg_1",
            turn_id="turn_2",
            thread_root_id=None,
            timestamp=datetime.now(UTC),
            role="user",
            content="Telegram user message",
        )
        await db_context.message_history.add_message(
            interface_type="telegram",
            conversation_id="tg_conv_filter_test",
            interface_message_id="tg_msg_2",
            turn_id="turn_2",
            thread_root_id=None,
            timestamp=datetime.now(UTC),
            role="assistant",
            content="Telegram assistant response",
        )

    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Test no filter (should return both)
        response = await client.get("/api/v1/chat/conversations")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        conversation_ids = [conv["conversation_id"] for conv in data["conversations"]]
        assert "web_conv_filter_test" in conversation_ids
        assert "tg_conv_filter_test" in conversation_ids

        # Test web filter (should return only web)
        response = await client.get("/api/v1/chat/conversations?interface_type=web")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["conversations"][0]["conversation_id"] == "web_conv_filter_test"

        # Test telegram filter (should return only telegram)
        response = await client.get(
            "/api/v1/chat/conversations?interface_type=telegram"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["conversations"][0]["conversation_id"] == "tg_conv_filter_test"

        # Test non-existent interface filter (should return empty)
        response = await client.get("/api/v1/chat/conversations?interface_type=api")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 0
        assert data["conversations"] == []


@pytest.mark.asyncio
async def test_get_conversations_conversation_id_filter(
    web_only_assistant: Assistant,
    db_engine: AsyncEngine,
) -> None:
    """Test filtering conversations by specific conversation ID."""
    # Create test conversations
    async with get_db_context(db_engine) as db_context:
        for i in range(3):
            conv_id = f"conv_id_filter_test_{i}"
            await db_context.message_history.add_message(
                interface_type="web",
                conversation_id=conv_id,
                interface_message_id=f"msg_{i}",
                turn_id=f"turn_{i}",
                thread_root_id=None,
                timestamp=datetime.now(UTC),
                role="user",
                content=f"Test message {i}",
            )

    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Test specific conversation ID filter
        response = await client.get(
            "/api/v1/chat/conversations?conversation_id=conv_id_filter_test_1"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["conversations"][0]["conversation_id"] == "conv_id_filter_test_1"

        # Test non-existent conversation ID
        response = await client.get(
            "/api/v1/chat/conversations?conversation_id=non_existent_conv"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 0
        assert data["conversations"] == []


@pytest.mark.asyncio
async def test_get_conversations_date_filters(
    web_only_assistant: Assistant, db_engine: AsyncEngine
) -> None:
    """Test filtering conversations by date range."""

    # Create test conversations with different timestamps
    base_time = datetime.now(UTC)
    async with get_db_context(db_engine) as db_context:
        # Old conversation (3 days ago)
        await db_context.message_history.add_message(
            interface_type="web",
            conversation_id="old_conv",
            interface_message_id="old_msg",
            turn_id="turn_old",
            thread_root_id=None,
            timestamp=base_time - timedelta(days=3),
            role="user",
            content="Old message",
        )

        # Recent conversation (1 day ago)
        await db_context.message_history.add_message(
            interface_type="web",
            conversation_id="recent_conv",
            interface_message_id="recent_msg",
            turn_id="turn_recent",
            thread_root_id=None,
            timestamp=base_time - timedelta(days=1),
            role="user",
            content="Recent message",
        )

        # Today's conversation
        await db_context.message_history.add_message(
            interface_type="web",
            conversation_id="today_conv",
            interface_message_id="today_msg",
            turn_id="turn_today",
            thread_root_id=None,
            timestamp=base_time,
            role="user",
            content="Today's message",
        )

    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Test date_from filter (last 2 days)
        date_from = (base_time - timedelta(days=2)).strftime("%Y-%m-%d")
        response = await client.get(f"/api/v1/chat/conversations?date_from={date_from}")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        conversation_ids = [conv["conversation_id"] for conv in data["conversations"]]
        assert "recent_conv" in conversation_ids
        assert "today_conv" in conversation_ids
        assert "old_conv" not in conversation_ids

        # Test date_to filter (up to 2 days ago)
        date_to = (base_time - timedelta(days=2)).strftime("%Y-%m-%d")
        response = await client.get(f"/api/v1/chat/conversations?date_to={date_to}")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["conversations"][0]["conversation_id"] == "old_conv"

        # Test date range (yesterday only)
        date_yesterday = (base_time - timedelta(days=1)).strftime("%Y-%m-%d")
        response = await client.get(
            f"/api/v1/chat/conversations?date_from={date_yesterday}&date_to={date_yesterday}"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["conversations"][0]["conversation_id"] == "recent_conv"


@pytest.mark.asyncio
async def test_get_conversations_invalid_date_formats(
    web_only_assistant: Assistant,
) -> None:
    """Test error handling for invalid date formats."""
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Test invalid date_from format
        response = await client.get("/api/v1/chat/conversations?date_from=invalid-date")
        assert response.status_code == 400
        assert "Invalid date_from format" in response.json()["detail"]
        assert "Expected YYYY-MM-DD format" in response.json()["detail"]

        # Test invalid date_to format
        response = await client.get("/api/v1/chat/conversations?date_to=2024-13-45")
        assert response.status_code == 400
        assert "Invalid date_to format" in response.json()["detail"]
        assert "Expected YYYY-MM-DD format" in response.json()["detail"]


@pytest.mark.asyncio
async def test_get_conversations_combined_filters(
    web_only_assistant: Assistant,
    db_engine: AsyncEngine,
) -> None:
    """Test using multiple filters together."""

    base_time = datetime.now(UTC)

    # Create test data
    async with get_db_context(db_engine) as db_context:
        # Web conversation from yesterday matching all filters
        await db_context.message_history.add_message(
            interface_type="web",
            conversation_id="matching_conv",
            interface_message_id="match_msg",
            turn_id="turn_match",
            thread_root_id=None,
            timestamp=base_time - timedelta(days=1),
            role="user",
            content="Matching message",
        )

        # Telegram conversation from yesterday (wrong interface)
        await db_context.message_history.add_message(
            interface_type="telegram",
            conversation_id="wrong_interface_conv",
            interface_message_id="wrong_msg",
            turn_id="turn_wrong",
            thread_root_id=None,
            timestamp=base_time - timedelta(days=1),
            role="user",
            content="Wrong interface message",
        )

        # Web conversation from 3 days ago (wrong date)
        await db_context.message_history.add_message(
            interface_type="web",
            conversation_id="wrong_date_conv",
            interface_message_id="date_msg",
            turn_id="turn_date",
            thread_root_id=None,
            timestamp=base_time - timedelta(days=3),
            role="user",
            content="Wrong date message",
        )

    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Use combined filters: web interface + yesterday's date
        date_yesterday = (base_time - timedelta(days=1)).strftime("%Y-%m-%d")
        response = await client.get(
            f"/api/v1/chat/conversations?interface_type=web&date_from={date_yesterday}&date_to={date_yesterday}"
        )

        assert response.status_code == 200
        data = response.json()

        # Should only return the matching conversation
        assert data["count"] == 1
        assert data["conversations"][0]["conversation_id"] == "matching_conv"


@pytest.mark.asyncio
async def test_get_conversation_messages_pagination_default(
    web_only_assistant: Assistant,
    db_engine: AsyncEngine,
) -> None:
    """Test default behavior of message pagination (loads recent messages)."""
    conv_id = "test_pagination_default"

    # Create 100 messages with distinct timestamps
    async with get_db_context(db_engine) as db_context:
        base_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        for i in range(100):
            timestamp = base_time + timedelta(minutes=i)  # Each message 1 minute apart
            await db_context.message_history.add_message(
                interface_type="web",
                conversation_id=conv_id,
                interface_message_id=f"msg_{i}",
                turn_id=f"turn_{i}",
                thread_root_id=None,
                timestamp=timestamp,
                role="user",
                content=f"Message {i}",
            )

    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Default request (should get 50 most recent messages)
        response = await client.get(f"/api/v1/chat/conversations/{conv_id}/messages")

        assert response.status_code == 200
        data = response.json()

        # Should get 50 most recent messages (50-99)
        assert len(data["messages"]) == 50
        assert data["count"] == 50  # This is the count in current batch
        assert data["total_messages"] == 100  # Total messages in conversation
        assert data["has_more_before"] is True  # More older messages available
        assert data["has_more_after"] is False  # These are the most recent

        # Messages should be in chronological order (oldest to newest in batch)
        messages = data["messages"]
        assert "Message 50" in messages[0]["content"]  # Oldest in batch
        assert "Message 99" in messages[-1]["content"]  # Newest in batch


@pytest.mark.asyncio
async def test_get_conversation_messages_pagination_before(
    web_only_assistant: Assistant,
    db_engine: AsyncEngine,
) -> None:
    """Test loading messages before a specific timestamp."""
    conv_id = "test_pagination_before"

    # Create messages with known timestamps
    async with get_db_context(db_engine) as db_context:
        base_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        for i in range(20):
            timestamp = base_time.replace(minute=i)
            await db_context.message_history.add_message(
                interface_type="web",
                conversation_id=conv_id,
                interface_message_id=f"msg_{i}",
                turn_id=f"turn_{i}",
                thread_root_id=None,
                timestamp=timestamp,
                role="user",
                content=f"Message {i}",
            )

    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Get messages before minute 15 (should get messages 0-14, but limited to 10)
        before_timestamp = (
            base_time.replace(minute=15).isoformat().replace("+00:00", "Z")
        )
        response = await client.get(
            f"/api/v1/chat/conversations/{conv_id}/messages?before={before_timestamp}&limit=10"
        )

        assert response.status_code == 200
        data = response.json()

        # Should get 10 messages before timestamp (messages 5-14 since we get newest first)
        assert len(data["messages"]) == 10
        assert data["has_more_before"] is True  # More messages 0-4 available
        assert data["has_more_after"] is True  # Messages 15-19 are newer

        # Verify content - should be messages 5-14 in chronological order
        messages = data["messages"]
        assert "Message 5" in messages[0]["content"]  # Oldest in this batch
        assert "Message 14" in messages[-1]["content"]  # Newest in this batch


@pytest.mark.asyncio
async def test_get_conversation_messages_pagination_after(
    web_only_assistant: Assistant,
    db_engine: AsyncEngine,
) -> None:
    """Test loading messages after a specific timestamp."""
    conv_id = "test_pagination_after"

    # Create messages with known timestamps
    async with get_db_context(db_engine) as db_context:
        base_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        for i in range(20):
            timestamp = base_time.replace(minute=i)
            await db_context.message_history.add_message(
                interface_type="web",
                conversation_id=conv_id,
                interface_message_id=f"msg_{i}",
                turn_id=f"turn_{i}",
                thread_root_id=None,
                timestamp=timestamp,
                role="user",
                content=f"Message {i}",
            )

    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Get messages after minute 5 (should get messages 6-19, but limited to 10)
        after_timestamp = base_time.replace(minute=5).isoformat().replace("+00:00", "Z")
        response = await client.get(
            f"/api/v1/chat/conversations/{conv_id}/messages?after={after_timestamp}&limit=10"
        )

        assert response.status_code == 200
        data = response.json()

        # Should get 10 messages after timestamp (messages 6-15)
        assert len(data["messages"]) == 10
        assert data["has_more_before"] is True  # Messages 0-5 are older
        assert data["has_more_after"] is True  # Messages 16-19 are newer

        # Verify content - should be messages 6-15 in chronological order
        messages = data["messages"]
        assert "Message 6" in messages[0]["content"]  # Oldest in this batch
        assert "Message 15" in messages[-1]["content"]  # Newest in this batch


@pytest.mark.asyncio
async def test_get_conversation_messages_pagination_limit_zero(
    web_only_assistant: Assistant,
    db_engine: AsyncEngine,
) -> None:
    """Test backward compatibility with limit=0 (get all messages)."""
    conv_id = "test_pagination_limit_zero"

    # Create 10 messages
    async with get_db_context(db_engine) as db_context:
        base_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        for i in range(10):
            timestamp = base_time.replace(minute=i)
            await db_context.message_history.add_message(
                interface_type="web",
                conversation_id=conv_id,
                interface_message_id=f"msg_{i}",
                turn_id=f"turn_{i}",
                thread_root_id=None,
                timestamp=timestamp,
                role="user",
                content=f"Message {i}",
            )

    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Request with limit=0 should return all messages
        response = await client.get(
            f"/api/v1/chat/conversations/{conv_id}/messages?limit=0"
        )

        assert response.status_code == 200
        data = response.json()

        # Should get all 10 messages
        assert len(data["messages"]) == 10
        assert data["count"] == 10
        assert data["total_messages"] == 10
        assert data["has_more_before"] is False  # No pagination with limit=0
        assert data["has_more_after"] is False

        # Messages should be in chronological order
        messages = data["messages"]
        assert "Message 0" in messages[0]["content"]
        assert "Message 9" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_get_conversation_messages_invalid_timestamp(
    web_only_assistant: Assistant,
) -> None:
    """Test error handling for invalid timestamp formats."""
    conv_id = "test_invalid_timestamps"

    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Test invalid before timestamp
        response = await client.get(
            f"/api/v1/chat/conversations/{conv_id}/messages?before=invalid-timestamp"
        )
        assert response.status_code == 400
        assert "Invalid timestamp format" in response.json()["detail"]

        # Test invalid after timestamp
        response = await client.get(
            f"/api/v1/chat/conversations/{conv_id}/messages?after=not-a-date"
        )
        assert response.status_code == 400
        assert "Invalid timestamp format" in response.json()["detail"]


@pytest.mark.asyncio
async def test_get_conversation_messages_empty_results(
    web_only_assistant: Assistant,
) -> None:
    """Test pagination with no messages matching criteria."""
    conv_id = "test_empty_pagination"

    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Request messages before a timestamp when no messages exist
        before_timestamp = (
            datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
        response = await client.get(
            f"/api/v1/chat/conversations/{conv_id}/messages?before={before_timestamp}"
        )

        assert response.status_code == 200
        data = response.json()

        # Should return empty results
        assert len(data["messages"]) == 0
        assert data["count"] == 0
        assert data["total_messages"] == 0
        assert data["has_more_before"] is False
        assert data["has_more_after"] is False
