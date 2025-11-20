"""Unit tests for message conversion in ProcessingService."""

import pytest

from family_assistant.llm.messages import AssistantMessage
from family_assistant.llm.tool_call import ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig


@pytest.fixture
def mock_processing_service() -> ProcessingService:
    """Create a minimal ProcessingService for testing message conversion."""
    config = ProcessingServiceConfig(
        prompts={"system_prompt": "Test"},
        timezone_str="UTC",
        max_history_messages=10,
        history_max_age_hours=24,
        delegation_security_level="high",
        tools_config={},
        id="test",
    )
    # We don't need real dependencies for this unit test
    return ProcessingService(
        llm_client=None,  # type: ignore[arg-type]
        tools_provider=None,  # type: ignore[arg-type]
        service_config=config,
        context_providers=[],
        server_url="http://test",
        app_config={},
    )


def test_convert_dict_messages_preserves_provider_metadata(
    mock_processing_service: ProcessingService,
) -> None:
    """Test that _convert_dict_messages_to_typed preserves provider_metadata in tool calls.

    This is critical for Gemini thinking models which require thought_signature
    to be preserved in the conversation history for multi-turn conversations.
    """
    # Simulate a message dict from database with tool call containing provider_metadata
    messages_dict = [
        {
            "role": "user",
            "content": "use Python to calculate 1+1",
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_abc123",
                    "type": "function",
                    "function": {
                        "name": "execute_python_code",
                        "arguments": '{"code": "1+1"}',
                    },
                    "provider_metadata": {
                        "provider": "google",
                        "thought_signature": "base64_encoded_signature_here",
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_abc123",
            "content": "2",
            "name": "execute_python_code",
        },
    ]

    # Convert to typed messages
    typed_messages = mock_processing_service._convert_dict_messages_to_typed(
        messages_dict
    )

    # Verify the assistant message has tool call with provider_metadata preserved
    assert len(typed_messages) == 3
    assert isinstance(typed_messages[1], AssistantMessage)
    assert typed_messages[1].tool_calls is not None
    assert len(typed_messages[1].tool_calls) == 1

    tool_call = typed_messages[1].tool_calls[0]
    assert isinstance(tool_call, ToolCallItem)
    assert tool_call.provider_metadata is not None
    assert tool_call.provider_metadata["provider"] == "google"
    assert tool_call.provider_metadata["thought_signature"] == (
        "base64_encoded_signature_here"
    )


def test_convert_dict_messages_handles_missing_provider_metadata(
    mock_processing_service: ProcessingService,
) -> None:
    """Test that conversion works when provider_metadata is absent."""
    messages_dict = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_xyz789",
                    "type": "function",
                    "function": {
                        "name": "some_tool",
                        "arguments": "{}",
                    },
                    # No provider_metadata
                }
            ],
        }
    ]

    typed_messages = mock_processing_service._convert_dict_messages_to_typed(
        messages_dict
    )

    assert len(typed_messages) == 1
    assert isinstance(typed_messages[0], AssistantMessage)
    assert typed_messages[0].tool_calls is not None
    assert len(typed_messages[0].tool_calls) == 1
    assert typed_messages[0].tool_calls[0].provider_metadata is None
