"""Integration tests for Gemini thought signature handling.

These tests use VCR.py to record real API interactions with Gemini's thinking models.
To record new cassettes, run with a valid GEMINI_API_KEY.
"""

import base64
import os

import pytest
import pytest_asyncio

from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient

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

    messages = [{"role": "user", "content": "What's the weather in San Francisco?"}]

    # Act: Call the real Gemini API
    response = await google_client_thinking.generate_response(
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

    messages = [{"role": "user", "content": "What is 2+2? Think step by step."}]

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
        {"role": "user", "content": "First message"},
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
        {
            "role": "tool",
            "tool_call_id": "call_123",
            "content": "Weather in Paris: 15°C, cloudy",
        },
        {"role": "user", "content": "Thanks! What about London?"},
    ]

    # Act: Convert messages to Gemini format (this should reconstruct thought signatures)
    genai_contents = google_client_thinking._convert_messages_to_genai_format(messages)

    # Assert: Verify signature was reconstructed in the assistant message
    model_messages = [msg for msg in genai_contents if msg.get("role") == "model"]
    assert len(model_messages) > 0

    # Find the message with tool calls (should be the assistant message)
    model_msg_with_tool = next((msg for msg in model_messages if "parts" in msg), None)
    assert model_msg_with_tool is not None

    # Check that at least one part has the reconstructed thought signature
    parts = model_msg_with_tool["parts"]
    parts_with_thought = [part for part in parts if "thought" in part]

    if parts_with_thought:
        # Verify the thought was reconstructed as bytes
        assert parts_with_thought[0]["thought"] == b"test_signature_data"
