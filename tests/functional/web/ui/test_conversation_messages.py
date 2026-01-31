"""Test /api/v1/chat/conversations/{id}/messages endpoint for getting conversation messages."""

import uuid
from datetime import UTC, datetime

import httpx
import pytest

from family_assistant.assistant import Assistant
from tests.helpers import wait_for_condition
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient


@pytest.mark.asyncio
async def test_get_conversation_messages_empty(web_only_assistant: Assistant) -> None:
    """Test getting messages for a non-existent conversation."""
    conv_id = f"non_existent_conv_{uuid.uuid4().hex[:8]}"

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
    web_only_assistant: Assistant,
    mock_llm_client: RuleBasedMockLLMClient,
) -> None:
    """Test getting messages for an existing conversation."""
    conv_id = f"test_conv_messages_{uuid.uuid4().hex[:8]}"

    # Configure mock LLM to respond with simple messages
    mock_llm_client.rules = [
        (
            lambda kwargs: True,
            lambda kwargs: LLMOutput(content="Hello! How can I help?"),
        ),
    ]

    # Create conversation via API
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Send a message
        response = await client.post(
            "/api/v1/chat/send_message",
            json={
                "prompt": "Hi there",
                "conversation_id": conv_id,
                "interface_type": "web",
            },
        )
        assert response.status_code == 200
        assert "Hello! How can I help?" in response.json()["reply"]

        # Get conversation messages via API with retry for eventual consistency
        result_data: dict = {}

        async def get_messages_with_both_roles() -> bool:
            nonlocal result_data
            resp = await client.get(f"/api/v1/chat/conversations/{conv_id}/messages")
            if resp.status_code == 200:
                result_data = resp.json()
                messages = result_data.get("messages", [])
                user_msgs = [m for m in messages if m["role"] == "user"]
                assistant_msgs = [m for m in messages if m["role"] == "assistant"]
                if user_msgs and assistant_msgs:
                    return True
            return False

        await wait_for_condition(
            get_messages_with_both_roles,
            timeout_seconds=30.0,
            error_message="both user and assistant messages not visible",
        )
        data = result_data

        # Should have 2 messages: user + assistant
        assert data["conversation_id"] == conv_id
        assert len(data["messages"]) >= 2
        assert data["count"] >= 2

        # Verify message roles and content
        messages = data["messages"]
        user_msgs = [m for m in messages if m["role"] == "user"]
        assistant_msgs = [m for m in messages if m["role"] == "assistant"]

        assert len(user_msgs) >= 1
        assert len(assistant_msgs) >= 1
        assert any("Hi there" in m["content"] for m in user_msgs)
        assert any("Hello! How can I help?" in m["content"] for m in assistant_msgs)


@pytest.mark.asyncio
async def test_get_conversation_messages_cross_interface_retrieval(
    web_only_assistant: Assistant,
    mock_llm_client: RuleBasedMockLLMClient,
) -> None:
    """Test that messages from multiple interactions are all retrieved for the same conversation ID."""
    conv_id = f"test_conv_interface_filter_{uuid.uuid4().hex[:8]}"

    # Configure mock LLM to respond with numbered responses
    call_count = 0

    def numbered_response(kwargs: dict) -> LLMOutput:
        nonlocal call_count
        call_count += 1
        return LLMOutput(content=f"Response {call_count}")

    mock_llm_client.rules = [(lambda kwargs: True, numbered_response)]

    # Create messages via API
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Send two messages to create a conversation with multiple exchanges
        response = await client.post(
            "/api/v1/chat/send_message",
            json={
                "prompt": "First message",
                "conversation_id": conv_id,
                "interface_type": "web",
            },
        )
        assert response.status_code == 200

        response = await client.post(
            "/api/v1/chat/send_message",
            json={
                "prompt": "Second message",
                "conversation_id": conv_id,
                "interface_type": "web",
            },
        )
        assert response.status_code == 200

        # Get messages via API with retry for eventual consistency
        result_data: dict = {}

        async def get_all_four_messages() -> bool:
            nonlocal result_data
            resp = await client.get(f"/api/v1/chat/conversations/{conv_id}/messages")
            if resp.status_code == 200:
                result_data = resp.json()
                if len(result_data.get("messages", [])) == 4:
                    return True
            return False

        await wait_for_condition(
            get_all_four_messages,
            timeout_seconds=30.0,
            error_message="expected 4 messages (2 user + 2 assistant) not visible",
        )
        data = result_data

        # Should have 4 messages (2 user + 2 assistant)
        assert len(data["messages"]) == 4
        assert data["count"] == 4
        assert data["total_messages"] == 4

        # Check that we have both user messages
        contents = [msg["content"] for msg in data["messages"]]
        assert any("First message" in c for c in contents)
        assert any("Second message" in c for c in contents)


@pytest.mark.asyncio
async def test_get_conversation_messages_pagination_default(
    web_only_assistant: Assistant,
    mock_llm_client: RuleBasedMockLLMClient,
) -> None:
    """Test default behavior of message pagination (loads recent messages)."""
    conv_id = f"test_pagination_default_{uuid.uuid4().hex[:8]}"

    # Configure mock LLM to respond with numbered responses
    call_count = 0

    def numbered_response(kwargs: dict) -> LLMOutput:
        nonlocal call_count
        call_count += 1
        return LLMOutput(content=f"Response {call_count}")

    mock_llm_client.rules = [(lambda kwargs: True, numbered_response)]

    # Create 100 messages via API (50 sends = 50 user + 50 assistant = 100 messages)
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        for i in range(50):
            response = await client.post(
                "/api/v1/chat/send_message",
                json={
                    "prompt": f"Message {i}",
                    "conversation_id": conv_id,
                    "interface_type": "web",
                },
            )
            assert response.status_code == 200, f"Failed to send message {i}"

        # Get messages via API - default limit is 50
        # Wait for all 100 messages to be visible before checking pagination
        result_data: dict = {}

        async def get_all_hundred_messages() -> bool:
            nonlocal result_data
            resp = await client.get(
                f"/api/v1/chat/conversations/{conv_id}/messages?limit=0"
            )
            if resp.status_code == 200:
                result_data = resp.json()
                if result_data.get("total_messages", 0) == 100:
                    return True
            return False

        await wait_for_condition(
            get_all_hundred_messages,
            timeout_seconds=30.0,
            error_message="expected 100 total messages not visible",
        )

        # Now test the default pagination behavior
        response = await client.get(f"/api/v1/chat/conversations/{conv_id}/messages")
        assert response.status_code == 200
        data = response.json()

        # Should get 50 most recent messages
        assert len(data["messages"]) == 50
        assert data["count"] == 50
        assert data["total_messages"] == 100
        assert data["has_more_before"] is True  # More older messages available
        assert data["has_more_after"] is False  # These are the most recent

        # Messages should be in chronological order (oldest to newest in batch)
        messages = data["messages"]
        # The most recent 50 messages should include the last user and assistant messages
        assert messages[-1]["role"] == "assistant"
        assert "Response 50" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_get_conversation_messages_pagination_before(
    web_only_assistant: Assistant,
    mock_llm_client: RuleBasedMockLLMClient,
) -> None:
    """Test loading messages before a specific timestamp."""
    conv_id = f"test_pagination_before_{uuid.uuid4().hex[:8]}"

    # Configure mock LLM to respond with numbered responses
    call_count = 0

    def numbered_response(kwargs: dict) -> LLMOutput:
        nonlocal call_count
        call_count += 1
        return LLMOutput(content=f"Response {call_count}")

    mock_llm_client.rules = [(lambda kwargs: True, numbered_response)]

    # Create 20 messages via API (10 sends = 10 user + 10 assistant = 20 messages)
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        for i in range(10):
            response = await client.post(
                "/api/v1/chat/send_message",
                json={
                    "prompt": f"Message {i}",
                    "conversation_id": conv_id,
                    "interface_type": "web",
                },
            )
            assert response.status_code == 200, f"Failed to send message {i}"

        # Wait for all 20 messages to be visible
        result_data: dict = {}

        async def get_all_twenty_messages() -> bool:
            nonlocal result_data
            resp = await client.get(
                f"/api/v1/chat/conversations/{conv_id}/messages?limit=0"
            )
            if resp.status_code == 200:
                result_data = resp.json()
                if len(result_data.get("messages", [])) == 20:
                    return True
            return False

        await wait_for_condition(
            get_all_twenty_messages,
            timeout_seconds=30.0,
            error_message="expected 20 messages not visible",
        )
        all_messages = result_data["messages"]
        assert len(all_messages) == 20

        # Use the timestamp of message at index 14 (15th message) as the "before" cutoff
        # This should return messages 0-13 when we ask for messages before this timestamp
        before_timestamp = all_messages[14]["timestamp"]

        # Get messages before the cutoff with limit 10
        response = await client.get(
            f"/api/v1/chat/conversations/{conv_id}/messages?before={before_timestamp}&limit=10"
        )
        assert response.status_code == 200
        data = response.json()

        # Should get 10 messages before the timestamp
        assert len(data["messages"]) == 10
        assert data["has_more_before"] is True  # More messages before
        assert data["has_more_after"] is True  # Messages after the cutoff

        # Messages should be in chronological order
        messages = data["messages"]
        # Verify we got messages from before the cutoff
        for msg in messages:
            assert msg["timestamp"] < before_timestamp


@pytest.mark.asyncio
async def test_get_conversation_messages_pagination_after(
    web_only_assistant: Assistant,
    mock_llm_client: RuleBasedMockLLMClient,
) -> None:
    """Test loading messages after a specific timestamp."""
    conv_id = f"test_pagination_after_{uuid.uuid4().hex[:8]}"

    # Configure mock LLM to respond with numbered responses
    call_count = 0

    def numbered_response(kwargs: dict) -> LLMOutput:
        nonlocal call_count
        call_count += 1
        return LLMOutput(content=f"Response {call_count}")

    mock_llm_client.rules = [(lambda kwargs: True, numbered_response)]

    # Create 20 messages via API (10 sends = 10 user + 10 assistant = 20 messages)
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        for i in range(10):
            response = await client.post(
                "/api/v1/chat/send_message",
                json={
                    "prompt": f"Message {i}",
                    "conversation_id": conv_id,
                    "interface_type": "web",
                },
            )
            assert response.status_code == 200, f"Failed to send message {i}"

        # Wait for all 20 messages to be visible
        result_data: dict = {}

        async def get_all_twenty_messages() -> bool:
            nonlocal result_data
            resp = await client.get(
                f"/api/v1/chat/conversations/{conv_id}/messages?limit=0"
            )
            if resp.status_code == 200:
                result_data = resp.json()
                if len(result_data.get("messages", [])) == 20:
                    return True
            return False

        await wait_for_condition(
            get_all_twenty_messages,
            timeout_seconds=30.0,
            error_message="expected 20 messages not visible",
        )
        all_messages = result_data["messages"]
        assert len(all_messages) == 20

        # Use the timestamp of message at index 5 (6th message) as the "after" cutoff
        # This should return messages 6-19 when we ask for messages after this timestamp
        after_timestamp = all_messages[5]["timestamp"]

        # Get messages after the cutoff with limit 10
        response = await client.get(
            f"/api/v1/chat/conversations/{conv_id}/messages?after={after_timestamp}&limit=10"
        )
        assert response.status_code == 200
        data = response.json()

        # Should get 10 messages after the timestamp
        assert len(data["messages"]) == 10
        assert data["has_more_before"] is True  # Messages before the cutoff
        assert data["has_more_after"] is True  # More messages after

        # Messages should be in chronological order
        messages = data["messages"]
        # Verify we got messages from after the cutoff
        for msg in messages:
            assert msg["timestamp"] > after_timestamp


@pytest.mark.asyncio
async def test_get_conversation_messages_pagination_limit_zero(
    web_only_assistant: Assistant,
    mock_llm_client: RuleBasedMockLLMClient,
) -> None:
    """Test backward compatibility with limit=0 (get all messages)."""
    conv_id = f"test_pagination_limit_zero_{uuid.uuid4().hex[:8]}"

    # Configure mock LLM to respond with numbered responses
    message_count = 0

    def numbered_response(kwargs: dict) -> LLMOutput:
        nonlocal message_count
        message_count += 1
        return LLMOutput(content=f"Response {message_count}")

    mock_llm_client.rules = [(lambda kwargs: True, numbered_response)]

    # Create messages via API
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Send 5 messages (each creates a user message + assistant response = 10 total)
        for i in range(5):
            response = await client.post(
                "/api/v1/chat/send_message",
                json={
                    "prompt": f"Message {i}",
                    "conversation_id": conv_id,
                    "interface_type": "web",
                },
            )
            assert response.status_code == 200, f"Failed to send message {i}"

        # Wait for all 10 messages to be visible
        result_data: dict = {}

        async def get_all_ten_messages() -> bool:
            nonlocal result_data
            resp = await client.get(
                f"/api/v1/chat/conversations/{conv_id}/messages?limit=0"
            )
            if resp.status_code == 200:
                result_data = resp.json()
                if len(result_data.get("messages", [])) == 10:
                    return True
            return False

        await wait_for_condition(
            get_all_ten_messages,
            timeout_seconds=30.0,
            error_message="expected 10 messages not visible",
        )
        data = result_data

        # Should get all 10 messages (5 user + 5 assistant)
        assert len(data["messages"]) == 10
        assert data["count"] == 10
        assert data["total_messages"] == 10
        assert data["has_more_before"] is False  # No pagination with limit=0
        assert data["has_more_after"] is False

        # Messages should be in chronological order
        messages = data["messages"]
        # First message should be user's "Message 0"
        assert messages[0]["role"] == "user"
        assert "Message 0" in messages[0]["content"]
        # Last message should be assistant's response
        assert messages[-1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_get_conversation_messages_invalid_timestamp(
    web_only_assistant: Assistant,
) -> None:
    """Test error handling for invalid timestamp formats."""
    conv_id = f"test_invalid_timestamps_{uuid.uuid4().hex[:8]}"

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
    conv_id = f"test_empty_pagination_{uuid.uuid4().hex[:8]}"

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
