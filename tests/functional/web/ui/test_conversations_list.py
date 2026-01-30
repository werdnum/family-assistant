"""Test /api/v1/chat/conversations endpoint for listing conversations."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.assistant import Assistant
from family_assistant.storage.context import get_db_context
from tests.functional.web.ui.conftest import wait_for_condition
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
                last_message = messages[-1].content or ""
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

        # Now get conversations via API (use retry for SQLite transaction visibility)
        # Check for 3 conversations, each with 2 messages (user + assistant)
        async def get_conversations() -> dict | None:
            response = await client.get("/api/v1/chat/conversations")
            if response.status_code == 200:
                data = response.json()
                convs = data["conversations"]
                if len(convs) == 3 and all(c["message_count"] == 2 for c in convs):
                    return data
            return None

        data = await wait_for_condition(
            get_conversations,
            description="3 conversations with 2 messages each to be visible",
        )
        assert data is not None
        assert data["count"] == 3

        # Check conversation summaries
        for conv in data["conversations"]:
            assert conv["conversation_id"] in conv_ids
            assert conv["last_message"].startswith("Hello! This is response")
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
async def test_get_conversations_interface_filter(
    web_only_assistant: Assistant,
    db_engine: AsyncEngine,
) -> None:
    """Test filtering conversations by interface type."""
    # Use fixed timestamps for deterministic behavior
    base_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)

    # Create test conversations in different interfaces
    async with get_db_context(db_engine) as db_context:
        # Add web conversation
        result1 = await db_context.message_history.add_message(
            interface_type="web",
            conversation_id="web_conv_filter_test",
            interface_message_id="web_msg_1",
            turn_id="turn_1",
            thread_root_id=None,
            timestamp=base_time,
            role="user",
            content="Web user message",
        )
        assert result1 is not None, "Failed to add web user message"

        result2 = await db_context.message_history.add_message(
            interface_type="web",
            conversation_id="web_conv_filter_test",
            interface_message_id="web_msg_2",
            turn_id="turn_1",
            thread_root_id=None,
            timestamp=base_time + timedelta(seconds=1),
            role="assistant",
            content="Web assistant response",
        )
        assert result2 is not None, "Failed to add web assistant message"

        # Add telegram conversation
        result3 = await db_context.message_history.add_message(
            interface_type="telegram",
            conversation_id="tg_conv_filter_test",
            interface_message_id="tg_msg_1",
            turn_id="turn_2",
            thread_root_id=None,
            timestamp=base_time + timedelta(seconds=2),
            role="user",
            content="Telegram user message",
        )
        assert result3 is not None, "Failed to add telegram user message"

        result4 = await db_context.message_history.add_message(
            interface_type="telegram",
            conversation_id="tg_conv_filter_test",
            interface_message_id="tg_msg_2",
            turn_id="turn_2",
            thread_root_id=None,
            timestamp=base_time + timedelta(seconds=3),
            role="assistant",
            content="Telegram assistant response",
        )
        assert result4 is not None, "Failed to add telegram assistant message"

    # Get conversations via API with retry for SQLite transaction visibility
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:

        async def get_conversations() -> dict | None:
            response = await client.get("/api/v1/chat/conversations")
            if response.status_code == 200:
                data = response.json()
                if data["count"] == 2:
                    return data
            return None

        data = await wait_for_condition(
            get_conversations, description="2 conversations to be visible"
        )
        assert data is not None
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
    # Use fixed timestamps for deterministic behavior
    base_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)

    # Create test conversations
    async with get_db_context(db_engine) as db_context:
        for i in range(3):
            conv_id = f"conv_id_filter_test_{i}"
            result = await db_context.message_history.add_message(
                interface_type="web",
                conversation_id=conv_id,
                interface_message_id=f"msg_{i}",
                turn_id=f"turn_{i}",
                thread_root_id=None,
                timestamp=base_time + timedelta(minutes=i),
                role="user",
                content=f"Test message {i}",
            )
            assert result is not None, f"Failed to add message for conv {i}"

    # Get conversations via API with retry for SQLite transaction visibility
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:

        async def get_conversations() -> dict | None:
            response = await client.get(
                "/api/v1/chat/conversations?conversation_id=conv_id_filter_test_1"
            )
            if response.status_code == 200:
                data = response.json()
                if data["count"] == 1:
                    return data
            return None

        data = await wait_for_condition(
            get_conversations, description="conversation to be visible"
        )
        assert data is not None
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

    # Use fixed base time for deterministic timestamps
    base_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    async with get_db_context(db_engine) as db_context:
        # Old conversation (3 days ago)
        result1 = await db_context.message_history.add_message(
            interface_type="web",
            conversation_id="old_conv",
            interface_message_id="old_msg",
            turn_id="turn_old",
            thread_root_id=None,
            timestamp=base_time - timedelta(days=3),
            role="user",
            content="Old message",
        )
        assert result1 is not None, "Failed to add old_conv message"

        # Recent conversation (1 day ago)
        result2 = await db_context.message_history.add_message(
            interface_type="web",
            conversation_id="recent_conv",
            interface_message_id="recent_msg",
            turn_id="turn_recent",
            thread_root_id=None,
            timestamp=base_time - timedelta(days=1),
            role="user",
            content="Recent message",
        )
        assert result2 is not None, "Failed to add recent_conv message"

        # Today's conversation
        result3 = await db_context.message_history.add_message(
            interface_type="web",
            conversation_id="today_conv",
            interface_message_id="today_msg",
            turn_id="turn_today",
            thread_root_id=None,
            timestamp=base_time,
            role="user",
            content="Today's message",
        )
        assert result3 is not None, "Failed to add today_conv message"

    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Wait for all 3 conversations to be visible (SQLite transaction visibility)
        async def get_all_conversations() -> dict | None:
            response = await client.get("/api/v1/chat/conversations")
            if response.status_code == 200:
                data = response.json()
                if data["count"] == 3:
                    return data
            return None

        await wait_for_condition(
            get_all_conversations, description="3 conversations to be visible"
        )

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

    # Use fixed base time for deterministic timestamps
    base_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)

    # Create test data
    async with get_db_context(db_engine) as db_context:
        # Web conversation from yesterday matching all filters
        result1 = await db_context.message_history.add_message(
            interface_type="web",
            conversation_id="matching_conv",
            interface_message_id="match_msg",
            turn_id="turn_match",
            thread_root_id=None,
            timestamp=base_time - timedelta(days=1),
            role="user",
            content="Matching message",
        )
        assert result1 is not None, "Failed to add matching_conv message"

        # Telegram conversation from yesterday (wrong interface)
        result2 = await db_context.message_history.add_message(
            interface_type="telegram",
            conversation_id="wrong_interface_conv",
            interface_message_id="wrong_msg",
            turn_id="turn_wrong",
            thread_root_id=None,
            timestamp=base_time - timedelta(days=1) + timedelta(minutes=1),
            role="user",
            content="Wrong interface message",
        )
        assert result2 is not None, "Failed to add wrong_interface_conv message"

        # Web conversation from 3 days ago (wrong date)
        result3 = await db_context.message_history.add_message(
            interface_type="web",
            conversation_id="wrong_date_conv",
            interface_message_id="date_msg",
            turn_id="turn_date",
            thread_root_id=None,
            timestamp=base_time - timedelta(days=3),
            role="user",
            content="Wrong date message",
        )
        assert result3 is not None, "Failed to add wrong_date_conv message"

    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Wait for all 3 conversations to be visible (SQLite transaction visibility)
        async def get_all_conversations() -> dict | None:
            response = await client.get("/api/v1/chat/conversations")
            if response.status_code == 200:
                data = response.json()
                if data["count"] == 3:
                    return data
            return None

        await wait_for_condition(
            get_all_conversations, description="3 conversations to be visible"
        )

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
