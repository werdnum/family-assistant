"""Test conversation history endpoints for the chat API."""

import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from family_assistant.assistant import Assistant
from family_assistant.storage.context import get_db_context
from family_assistant.web.app_creator import app as fastapi_app


@pytest.mark.asyncio
async def test_get_conversations_empty(web_only_assistant: Assistant) -> None:
    """Test getting conversations when none exist."""
    # Create HTTP client using the configured fastapi app
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/chat/conversations")

        assert response.status_code == 200
        data = response.json()
        assert data["conversations"] == []
        assert data["total"] == 0


@pytest.mark.asyncio
async def test_get_conversations_with_data(web_only_assistant: Assistant) -> None:
    """Test getting conversations with existing conversation data."""
    # Get database context using the storage engine
    async with get_db_context() as db_context:
        # Create some test conversations
        conv_ids = [f"test_conv_{i}" for i in range(3)]
        base_time = datetime.now(timezone.utc) - timedelta(hours=3)

        for i, conv_id in enumerate(conv_ids):
            # Add messages to each conversation
            await db_context.message_history.add_message(
                interface_type="web",
                conversation_id=conv_id,
                interface_message_id=f"msg_{i}_1",
                turn_id=f"turn_{i}_1",
                thread_root_id=None,
                timestamp=base_time + timedelta(minutes=i * 10),
                role="user",
                content=f"Hello from conversation {i}",
            )

            await db_context.message_history.add_message(
                interface_type="web",
                conversation_id=conv_id,
                interface_message_id=f"msg_{i}_2",
                turn_id=f"turn_{i}_1",
                thread_root_id=None,
                timestamp=base_time + timedelta(minutes=i * 10 + 1),
                role="assistant",
                content=f"Hello! This is response {i}",
            )

    # Get conversations via API
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/chat/conversations")

        assert response.status_code == 200
        data = response.json()
        assert len(data["conversations"]) == 3
        assert data["total"] == 3

        # Check conversation summaries
        for conv in data["conversations"]:
            assert conv["conversation_id"] in conv_ids
            assert conv["last_message"].startswith("Hello! This is response")
            assert conv["message_count"] == 2
            assert "last_timestamp" in conv


@pytest.mark.asyncio
async def test_get_conversations_pagination(web_only_assistant: Assistant) -> None:
    """Test pagination of conversations list."""
    # Create test data
    async with get_db_context() as db_context:
        # Create 5 test conversations
        base_time = datetime.now(timezone.utc) - timedelta(hours=5)

        for i in range(5):
            conv_id = f"test_conv_page_{i}"
            await db_context.message_history.add_message(
                interface_type="web",
                conversation_id=conv_id,
                interface_message_id=f"msg_page_{i}",
                turn_id=f"turn_page_{i}",
                thread_root_id=None,
                timestamp=base_time + timedelta(minutes=i * 5),
                role="user",
                content=f"Message {i}",
            )

    # Test pagination via API
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Get first page (limit 2)
        response = await client.get("/api/v1/chat/conversations?limit=2&offset=0")
        assert response.status_code == 200
        data = response.json()
        assert len(data["conversations"]) == 2
        assert data["total"] == 5

        # Get second page
        response = await client.get("/api/v1/chat/conversations?limit=2&offset=2")
        assert response.status_code == 200
        data = response.json()
        assert len(data["conversations"]) == 2
        assert data["total"] == 5

        # Get third page
        response = await client.get("/api/v1/chat/conversations?limit=2&offset=4")
        assert response.status_code == 200
        data = response.json()
        assert len(data["conversations"]) == 1
        assert data["total"] == 5


@pytest.mark.asyncio
async def test_get_conversation_messages_empty(web_only_assistant: Assistant) -> None:
    """Test getting messages for a non-existent conversation."""
    conv_id = "non_existent_conv"

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(f"/api/v1/chat/conversations/{conv_id}/messages")

        assert response.status_code == 200
        data = response.json()
        assert data["conversation_id"] == conv_id
        assert data["messages"] == []
        assert data["total"] == 0


@pytest.mark.asyncio
async def test_get_conversation_messages_with_data(
    web_only_assistant: Assistant,
) -> None:
    """Test getting messages for an existing conversation."""
    conv_id = "test_conv_messages"

    # Create test messages
    async with get_db_context() as db_context:
        # Add various types of messages
        messages_data = [
            {
                "role": "user",
                "content": "What is the weather like?",
                "interface_message_id": "msg_1",
                "turn_id": "turn_1",
            },
            {
                "role": "assistant",
                "content": "I'll check the weather for you.",
                "interface_message_id": "msg_2",
                "turn_id": "turn_1",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"location": "current"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "content": "Weather: Sunny, 72°F",
                "interface_message_id": "msg_3",
                "turn_id": "turn_1",
                "tool_call_id": "call_1",
            },
            {
                "role": "assistant",
                "content": "The weather is sunny and 72°F.",
                "interface_message_id": "msg_4",
                "turn_id": "turn_1",
            },
        ]

        # Add messages to database
        base_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        for idx, msg_data in enumerate(messages_data):
            result = await db_context.message_history.add_message(
                interface_type="web",
                conversation_id=conv_id,
                timestamp=base_time + timedelta(seconds=idx * 30),
                thread_root_id=None,
                **msg_data,
            )
            # Store internal_id for validation
            if result:
                msg_data["internal_id"] = result["internal_id"]

    # Get conversation messages via API
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(f"/api/v1/chat/conversations/{conv_id}/messages")

        assert response.status_code == 200
        data = response.json()
        assert data["conversation_id"] == conv_id
        assert len(data["messages"]) == 4
        assert data["total"] == 4

        # Verify message details
        for i, msg in enumerate(data["messages"]):
            expected = messages_data[i]
            assert msg["internal_id"] == expected["internal_id"]
            assert msg["role"] == expected["role"]
            assert msg["content"] == expected["content"]
            assert "timestamp" in msg

            # Check tool calls if present
            if "tool_calls" in expected:
                assert msg["tool_calls"] == expected["tool_calls"]

            # Check tool_call_id for tool messages
            if expected["role"] == "tool":
                assert msg.get("tool_call_id") == expected["tool_call_id"]


@pytest.mark.asyncio
async def test_get_conversation_messages_filters_by_interface(
    web_only_assistant: Assistant,
) -> None:
    """Test that messages are filtered by interface type (web only)."""
    conv_id = "test_conv_interface_filter"

    # Create test messages
    async with get_db_context() as db_context:
        # Add web message
        await db_context.message_history.add_message(
            interface_type="web",
            conversation_id=conv_id,
            interface_message_id="web_msg",
            turn_id="turn_1",
            thread_root_id=None,
            timestamp=datetime.now(timezone.utc),
            role="user",
            content="Web message",
        )

        # Add telegram message (should not appear)
        await db_context.message_history.add_message(
            interface_type="telegram",
            conversation_id=conv_id,
            interface_message_id="tg_msg",
            turn_id="turn_2",
            thread_root_id=None,
            timestamp=datetime.now(timezone.utc),
            role="user",
            content="Telegram message",
        )

    # Get messages via API
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(f"/api/v1/chat/conversations/{conv_id}/messages")

        assert response.status_code == 200
        data = response.json()
        assert len(data["messages"]) == 1
        assert data["messages"][0]["content"] == "Web message"


@pytest.mark.asyncio
async def test_conversation_summary_performance(web_only_assistant: Assistant) -> None:
    """Test that conversation summaries are efficiently retrieved."""
    # Create many conversations with multiple messages each
    async with get_db_context() as db_context:
        num_conversations = 50
        messages_per_conversation = 20
        base_time = datetime.now(timezone.utc) - timedelta(days=1)

        for conv_idx in range(num_conversations):
            conv_id = f"perf_test_conv_{conv_idx}"
            conv_base_time = base_time + timedelta(minutes=conv_idx)

            for msg_idx in range(messages_per_conversation):
                role = "user" if msg_idx % 2 == 0 else "assistant"
                await db_context.message_history.add_message(
                    interface_type="web",
                    conversation_id=conv_id,
                    interface_message_id=f"msg_{conv_idx}_{msg_idx}",
                    turn_id=f"turn_{conv_idx}_{msg_idx // 2}",
                    thread_root_id=None,
                    timestamp=conv_base_time + timedelta(seconds=msg_idx),
                    role=role,
                    content=f"Message {msg_idx} in conversation {conv_idx}",
                )

    # Get first page of conversations - should be fast despite many messages
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        start_time = time.time()
        response = await client.get("/api/v1/chat/conversations?limit=10")
        elapsed_time = time.time() - start_time

        assert response.status_code == 200
        data = response.json()
        assert len(data["conversations"]) == 10
        assert data["total"] == num_conversations

        # Should complete quickly even with many messages
        # The optimized query should prevent loading all messages
        assert elapsed_time < 2.0, f"Query took too long: {elapsed_time}s"
