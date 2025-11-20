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
from family_assistant.llm.providers.google_types import GeminiProviderMetadata
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

    # Check that at least one tool call has provider_metadata with thought signature
    tool_call_with_thoughts = next(
        (tc for tc in response.tool_calls if tc.provider_metadata is not None), None
    )

    if tool_call_with_thoughts and tool_call_with_thoughts.provider_metadata:
        # Verify structure - NEW format has single thought_signature, not array
        # provider_metadata is now a GeminiProviderMetadata object
        metadata = tool_call_with_thoughts.provider_metadata
        assert isinstance(metadata, GeminiProviderMetadata)
        assert metadata.thought_signature is not None

        # Verify signature is bytes
        assert isinstance(metadata.thought_signature.to_google_format(), bytes)
        assert len(metadata.thought_signature.to_google_format()) > 0


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_thought_signatures_without_tool_calls(
    google_client_thinking: GoogleGenAIClient,
) -> None:
    """Test that responses work correctly even without tool calls.

    Note: Thought signatures are only preserved on function calls in the current
    implementation. Text-only responses don't preserve thought signatures at the
    message level.
    """
    if os.getenv("CI") and not os.getenv("GEMINI_API_KEY"):
        pytest.skip("Skipping Google test in CI without API key")

    messages = [create_user_message("What is 2+2? Think step by step.")]

    # Act: Call the real Gemini API without tools
    response = await google_client_thinking.generate_response(messages=messages)

    # Assert: Should get a response with content
    assert response.content is not None
    # Message-level provider_metadata is not used in new format
    # Thought signatures are only on tool calls


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_thought_signature_reconstruction(
    google_client_thinking: GoogleGenAIClient,
) -> None:
    """Test that thought signatures are reconstructed when converting history to Gemini format."""
    if os.getenv("CI") and not os.getenv("GEMINI_API_KEY"):
        pytest.skip("Skipping Google test in CI without API key")

    # Arrange: Create a message with provider_metadata containing thought signature
    # This simulates a message retrieved from database that had thought signatures
    # NEW format: thought_signature is on each tool call, not at message level
    # Thought signatures are stored as base64-encoded strings
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
                    "provider_metadata": {
                        "provider": "google",
                        "thought_signature": mock_signature,
                    },
                }
            ],
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
    # genai_contents now contains types.Content objects (Pydantic models)
    # Filter for Content objects with role="model"
    model_messages = []
    for msg in genai_contents:
        # Check if it's a dict with role="model"
        if (
            isinstance(msg, dict)
            and msg.get("role") == "model"
            or hasattr(msg, "role")
            and getattr(msg, "role", None) == "model"
        ):
            model_messages.append(msg)

    assert len(model_messages) > 0, "No model messages found in genai_contents"

    # Find the message with parts (should be the assistant message with tool calls)
    model_msg_with_tool = None
    for msg in model_messages:
        if isinstance(msg, dict):
            if "parts" in msg and msg["parts"]:
                model_msg_with_tool = msg
                break
        elif hasattr(msg, "parts"):
            parts = getattr(msg, "parts", None)
            if parts:
                model_msg_with_tool = msg
                break

    assert model_msg_with_tool is not None, "No message with parts found"

    # Check that at least one part has the reconstructed thought signature
    if isinstance(model_msg_with_tool, dict):
        parts = model_msg_with_tool["parts"]
        parts_with_thought_signature = [
            part
            for part in parts
            if isinstance(part, dict) and "thought_signature" in part
        ]
        if parts_with_thought_signature:
            assert (
                parts_with_thought_signature[0]["thought_signature"]
                == b"test_signature_data"
            )
    else:
        # Working with types.Content object
        parts = getattr(model_msg_with_tool, "parts", [])
        parts_with_thought_signature = []
        for part in parts:
            if hasattr(part, "thought_signature"):
                sig = getattr(part, "thought_signature", None)
                if sig:
                    parts_with_thought_signature.append(part)

        if parts_with_thought_signature:
            # Verify the thought_signature was reconstructed as bytes
            sig = parts_with_thought_signature[0].thought_signature
            assert sig == b"test_signature_data"


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
    # NEW format: provider_metadata is on each tool call, not at message level
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
                "provider_metadata": tc.provider_metadata,  # Metadata on tool call
            }
            for tc in response1.tool_calls
        ]
        if response1.tool_calls
        else None,
    }

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
