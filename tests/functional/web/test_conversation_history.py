"""Test conversation history endpoints for the chat API."""

import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from family_assistant.assistant import Assistant
from family_assistant.storage.context import get_db_context
from family_assistant.web.app_creator import app as fastapi_app
from tests.mocks.mock_llm import RuleBasedMockLLMClient


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
async def test_get_conversations_with_data(
    web_only_assistant: Assistant, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test getting conversations with existing conversation data."""
    # Configure mock LLM to respond appropriately
    from tests.mocks.mock_llm import LLMOutput

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

    transport = httpx.ASGITransport(app=fastapi_app)
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
        assert data["total"] == 3

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
    from tests.mocks.mock_llm import LLMOutput

    # Simple response for all messages
    mock_llm_client.rules = [
        (lambda kwargs: True, LLMOutput(content="Test response")),
    ]

    # Create 5 test conversations via API
    transport = httpx.ASGITransport(app=fastapi_app)
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
    web_only_assistant: Assistant, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test getting messages for an existing conversation."""
    import json

    from family_assistant.llm import ToolCallFunction, ToolCallItem
    from tests.mocks.mock_llm import LLMOutput

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
    transport = httpx.ASGITransport(app=fastapi_app)
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
        assert data["total"] >= 4

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
