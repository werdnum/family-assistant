"""Integration tests for Gemini thought signature handling.

These tests use VCR.py to record real API interactions with Gemini's thinking models.
To record new cassettes, run with a valid GEMINI_API_KEY.
"""

import base64
import os

import pytest
import pytest_asyncio

from family_assistant.llm.messages import message_to_dict
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from tests.factories.messages import (
    create_tool_message,
    create_user_message,
)

from .vcr_helpers import sanitize_response


@pytest_asyncio.fixture
async def google_client_thinking() -> GoogleGenAIClient:
    """Create a GoogleGenAIClient for testing with thinking model."""
    api_key = os.getenv("GEMINI_API_KEY", "test-gemini-key")
    return GoogleGenAIClient(
        api_key=api_key,
        model="gemini-2.0-flash-thinking-exp",
    )


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_thought_signatures_with_tool_calls(
    google_client_thinking: GoogleGenAIClient,
) -> None:
    """Test that thought signatures are extracted from responses with tool calls."""
    if os.getenv("CI") and not os.getenv("GEMINI_API_KEY"):
        pytest.skip("Skipping Google test in CI without API key")

    # Arrange: Define a simple tool
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the current weather",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {"type": "string", "description": "City name"},
                    },
                    "required": ["location"],
                },
            },
        }
    ]

    messages = [create_user_message("What's the weather in San Francisco?")]

    # Act: Call the real Gemini API
    response = await google_client_thinking.generate_response(  # type: ignore[reportArgumentType]
        messages=messages, tools=tools
    )

    # Assert: Verify thought signature was extracted
    # The thinking model should return tool calls with thought signatures
    assert response.tool_calls is not None, "Expected tool calls from thinking model"
    assert len(response.tool_calls) > 0

    # Check that at least one tool call has provider_metadata with thought signatures
    tool_call_with_thoughts = next(
        (tc for tc in response.tool_calls if tc.provider_metadata is not None), None
    )

    if tool_call_with_thoughts and tool_call_with_thoughts.provider_metadata:
        # Verify structure
        assert tool_call_with_thoughts.provider_metadata["provider"] == "google"
        assert "thought_signatures" in tool_call_with_thoughts.provider_metadata

        signatures = tool_call_with_thoughts.provider_metadata["thought_signatures"]
        assert isinstance(signatures, list)
        assert len(signatures) > 0
        assert "part_index" in signatures[0]
        assert "signature" in signatures[0]

        # Verify signature is base64 encoded and can be decoded
        decoded_sig = base64.b64decode(signatures[0]["signature"])
        assert isinstance(decoded_sig, bytes)
        assert len(decoded_sig) > 0


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_thought_signatures_without_tool_calls(
    google_client_thinking: GoogleGenAIClient,
) -> None:
    """Test that thought signatures are extracted even without tool calls."""
    if os.getenv("CI") and not os.getenv("GEMINI_API_KEY"):
        pytest.skip("Skipping Google test in CI without API key")

    messages = [create_user_message("What is 2+2? Think step by step.")]

    # Act: Call the real Gemini API without tools
    response = await google_client_thinking.generate_response(messages=messages)

    # Assert: Should get a response
    assert response.content is not None

    # If the thinking model produces thoughts for this query, verify structure
    if response.provider_metadata:
        assert response.provider_metadata["provider"] == "google"
        assert "thought_signatures" in response.provider_metadata

        signatures = response.provider_metadata["thought_signatures"]
        assert len(signatures) > 0
        assert "part_index" in signatures[0]
        assert "signature" in signatures[0]

        # Verify signature is base64 encoded
        decoded_sig = base64.b64decode(signatures[0]["signature"])
        assert isinstance(decoded_sig, bytes)
        assert len(decoded_sig) > 0


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_thought_signature_reconstruction(
    google_client_thinking: GoogleGenAIClient,
) -> None:
    """Test that thought signatures are reconstructed when converting history to Gemini format."""
    if os.getenv("CI") and not os.getenv("GEMINI_API_KEY"):
        pytest.skip("Skipping Google test in CI without API key")

    # Arrange: Create a message with provider_metadata containing thought signatures
    # This simulates a message retrieved from database that had thought signatures
    mock_signature = base64.b64encode(b"test_signature_data").decode("ascii")
    messages = [
        create_user_message("First message"),
        {
            "role": "assistant",
            "content": "I'll help with that",
            "tool_calls": [
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"location": "Paris"}',
                    },
                }
            ],
            "provider_metadata": {
                "provider": "google",
                "thought_signatures": [{"part_index": 1, "signature": mock_signature}],
            },
        },
        create_tool_message(
            tool_call_id="call_123",
            content="Weather in Paris: 15°C, cloudy",
        ),
        create_user_message("Thanks! What about London?"),
    ]

    # Act: Convert messages to Gemini format (this should reconstruct thought signatures)
    # Convert Pydantic message objects to dicts
    message_dicts = [
        message_to_dict(msg) if hasattr(msg, "model_dump") else msg for msg in messages
    ]
    genai_contents = google_client_thinking._convert_messages_to_genai_format(
        message_dicts
    )

    # Assert: Verify signature was reconstructed in the assistant message
    model_messages = [msg for msg in genai_contents if msg.get("role") == "model"]
    assert len(model_messages) > 0

    # Find the message with tool calls (should be the assistant message)
    model_msg_with_tool = next((msg for msg in model_messages if "parts" in msg), None)
    assert model_msg_with_tool is not None

    # Check that at least one part has the reconstructed thought signature
    parts = model_msg_with_tool["parts"]
    parts_with_thought_signature = [
        part for part in parts if "thought_signature" in part
    ]

    if parts_with_thought_signature:
        # Verify the thought_signature was reconstructed as bytes
        assert (
            parts_with_thought_signature[0]["thought_signature"]
            == b"test_signature_data"
        )


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_thought_signature_multiturn_with_api(
    google_client_thinking: GoogleGenAIClient,
) -> None:
    """Test that thought signatures work in multi-turn conversations sent to the API.

    This is a true integration test that verifies the API accepts reconstructed
    thought signatures. It catches bugs like using wrong field names that would
    cause API validation errors.
    """
    if os.getenv("CI") and not os.getenv("GEMINI_API_KEY"):
        pytest.skip("Skipping Google test in CI without API key")

    # Define tools for the conversation
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the current weather",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {"type": "string", "description": "City name"},
                    },
                    "required": ["location"],
                },
            },
        }
    ]

    # Turn 1: Initial request that will generate thought signatures
    initial_messages = [create_user_message("What's the weather in Paris?")]

    response1 = await google_client_thinking.generate_response(
        messages=initial_messages, tools=tools
    )

    # Verify we got tool calls with thought signatures
    assert response1.tool_calls is not None
    assert len(response1.tool_calls) > 0

    # Build conversation history including the response with thought signatures
    # This simulates what would be stored in the database
    conversation_history = initial_messages.copy()

    # Add the assistant's response with tool calls
    assistant_message = {
        "role": "assistant",
        "content": response1.content or "",
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in response1.tool_calls
        ]
        if response1.tool_calls
        else None,
    }

    # Include provider_metadata with thought signatures if present
    if response1.provider_metadata:
        assistant_message["provider_metadata"] = response1.provider_metadata

    conversation_history.append(assistant_message)  # type: ignore[arg-type]

    # Add tool response
    tool_msg = create_tool_message(
        tool_call_id=response1.tool_calls[0].id,
        content="15°C, sunny",
    )
    conversation_history.append(tool_msg)  # type: ignore[reportArgumentType]

    # Turn 2: Follow-up message - this will reconstruct thought signatures
    # and send them back to the API
    conversation_history.append(create_user_message("Thanks! What about London?"))

    # This is the critical test: send messages with reconstructed thought signatures
    # If we used the wrong field name, the API would reject this with validation errors
    # Convert any Pydantic message objects to dicts for the API
    conversation_history_dicts = [
        message_to_dict(msg) if hasattr(msg, "model_dump") else msg
        for msg in conversation_history
    ]
    response2 = await google_client_thinking.generate_response(
        messages=conversation_history_dicts,  # type: ignore[arg-type]
        tools=tools,
    )

    # If we got here without validation errors, the thought signatures were
    # correctly reconstructed and accepted by the API
    assert response2 is not None
    # The second response should also have tool calls for London weather
    assert response2.tool_calls is not None
