"""
Unit tests for the history formatting logic in ProcessingService.
"""

import base64
import json
from collections.abc import AsyncIterator
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from PIL import Image

from family_assistant.llm import LLMStreamEvent
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.services.attachments import AttachmentService
from family_assistant.tools.types import ToolExecutionContext


# Mock interfaces required by ProcessingService constructor
class MockLLMClient:
    async def generate_response(self, *args: Any, **kwargs: Any) -> Mock:  # noqa: ANN401 # Mock needs flexibility
        return Mock()  # Not used in the tested method

    def generate_response_stream(
        self,
        *args: Any,  # noqa: ANN401  # Mock needs flexibility
        **kwargs: Any,  # noqa: ANN401 # Mock needs flexibility
    ) -> AsyncIterator[LLMStreamEvent]:
        # Return an async generator that yields nothing
        async def empty_generator() -> AsyncIterator[LLMStreamEvent]:
            return
            yield  # Make it a generator

        return empty_generator()

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
        # Add any other params from the protocol if necessary, though not used here
        **_kwargs: Any,  # noqa: ANN401 # Capture other potential arguments from protocol
    ) -> dict[str, Any]:
        # Return a simple dict structure, content not important for these tests
        return {"role": "user", "content": prompt_text or ""}


class MockToolsProvider:
    async def get_tool_definitions(
        self,
        *args: Any,  # noqa: ANN401  # Mock needs flexibility
        **kwargs: Any,  # noqa: ANN401 # Mock needs flexibility
    ) -> list[dict[str, Any]]:
        return []  # Not used

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str:
        # Actual execution logic not needed for these tests, just conform to signature
        # The protocol expects a string return.
        return f"Executed tool {name} with args {arguments} in context {context}"

    async def close(self) -> None:
        pass  # Not used


# --- Test Setup ---


@pytest.fixture
def processing_service() -> ProcessingService:
    """Provides a ProcessingService instance with mock dependencies."""
    mock_service_config = ProcessingServiceConfig(
        prompts={},  # Not used by _format_history_for_llm
        timezone_str="UTC",  # Not used
        max_history_messages=10,  # Not used
        history_max_age_hours=1,  # Not used
        tools_config={},  # Added missing tools_config
        delegation_security_level="confirm",  # Added
        id="history_formatting_test_profile",  # Added
    )
    return ProcessingService(
        llm_client=MockLLMClient(),
        tools_provider=MockToolsProvider(),
        service_config=mock_service_config,  # Use the config object
        context_providers=[],
        server_url="http://test.com",  # Not used
        app_config={},  # Add dummy app_config
    )


# --- Test Cases ---


async def test_format_simple_history(processing_service: ProcessingService) -> None:
    """Test formatting a simple user-assistant conversation."""
    history_messages = [
        {"role": "user", "content": "Hello", "tool_calls_info_raw": None},
        {"role": "assistant", "content": "Hi there!", "tool_calls_info_raw": None},
    ]
    expected_output = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    actual_output = await processing_service._format_history_for_llm(history_messages)
    assert actual_output == expected_output  # Marked line 120


async def test_format_history_with_tool_call(
    processing_service: ProcessingService,
) -> None:
    """Test formatting history including an assistant message with a tool call."""
    tool_call_id = "call_123"
    tool_name = "get_weather"
    tool_args = {"location": "London"}
    tool_response = "Weather in London is sunny."

    history_messages = [
        {
            "role": "user",
            "content": "What's the weather like?",
            "tool_calls_info_raw": None,
        },
        {
            "role": "assistant",
            "content": None,  # Assistant might not provide text when calling tools
            "tool_calls": [  # Use the new key 'tool_calls' and OpenAI-like structure
                {
                    "id": tool_call_id,  # Corresponds to OpenAI tool_call 'id'
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(
                            tool_args
                        ),  # Arguments should be a JSON string in this format
                    },
                }
            ],
        },
        # This should represent the stored 'tool' response message
        {
            "role": "tool",
            "tool_call_id": tool_call_id,  # Required for tool messages
            "content": tool_response,  # The actual tool response content
        },
    ]

    expected_output = [
        {"role": "user", "content": "What's the weather like?"},
        # Assistant message requesting the tool
        {
            "role": "assistant",
            "tool_calls": [  # This should be passed through directly
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(tool_args),  # Arguments are stringified
                    },
                }
            ],
        },
        # Tool response message
        {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": tool_response,
        },
    ]

    actual_output = await processing_service._format_history_for_llm(history_messages)
    assert actual_output == expected_output


async def test_format_history_preserves_leading_tool_and_assistant_tool_calls(
    processing_service: ProcessingService,
) -> None:
    """
    Test that _format_history_for_llm correctly formats and preserves
    leading 'tool' messages and 'assistant' messages with 'tool_calls'.
    The actual pruning of such leading messages happens later in
    generate_llm_response_for_chat, which consumes the output of this method.
    """
    tool_call_id1 = "call_t1"
    tool_call_id2 = "call_a2"
    tool_name2 = "another_tool"
    tool_args2 = {"param": "value"}

    history_messages = [
        {
            "role": "tool",
            "tool_call_id": tool_call_id1,
            "content": "Response from first tool",
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": tool_call_id2,
                    "type": "function",
                    "function": {
                        "name": tool_name2,
                        "arguments": json.dumps(tool_args2),
                    },
                }
            ],
        },
        {"role": "user", "content": "Follow-up user message"},
    ]

    expected_output = [
        {
            "role": "tool",
            "tool_call_id": tool_call_id1,
            "content": "Response from first tool",
        },
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": tool_call_id2,
                    "type": "function",
                    "function": {
                        "name": tool_name2,
                        "arguments": json.dumps(tool_args2),
                    },
                }
            ],
        },
        {"role": "user", "content": "Follow-up user message"},
    ]

    actual_output = await processing_service._format_history_for_llm(history_messages)
    assert actual_output == expected_output


async def test_format_history_includes_errors_as_assistant(
    processing_service: ProcessingService,
) -> None:
    """Test that messages with role 'error' are included as assistant messages."""
    history_messages = [
        {"role": "user", "content": "Try something", "tool_calls_info_raw": None},
        {
            "role": "error",
            "content": "Something went wrong",
            "error_traceback": "Traceback...",
            "tool_calls_info_raw": None,
        },
        {"role": "assistant", "content": "Okay", "tool_calls_info_raw": None},
    ]
    expected_output = [
        {"role": "user", "content": "Try something"},
        {
            "role": "assistant",
            "content": "I encountered an error: Something went wrong\n\nError details: Traceback...",
        },
        {"role": "assistant", "content": "Okay"},
    ]
    actual_output = await processing_service._format_history_for_llm(history_messages)
    assert actual_output == expected_output


async def test_format_history_handles_empty_tool_calls(
    processing_service: ProcessingService,
) -> None:
    """Test formatting an assistant message where tool_calls_info is explicitly empty."""
    history_messages = [
        {"role": "user", "content": "User message", "tool_calls_info_raw": None},
        {
            "role": "assistant",
            "content": "Assistant message",
            "tool_calls_info_raw": [],
        },  # Empty list
    ]
    expected_output = [
        {"role": "user", "content": "User message"},
        {
            "role": "assistant",
            "content": "Assistant message",
        },  # Should be treated as simple message
    ]
    actual_output = await processing_service._format_history_for_llm(history_messages)
    assert actual_output == expected_output


async def test_format_history_converts_attachment_urls(
    processing_service: ProcessingService, tmp_path: Path
) -> None:
    """Test that attachment URLs are converted to data URIs."""

    # Create a mock attachment file
    attachment_id = "550e8400-e29b-41d4-a716-446655440000"
    storage_path = tmp_path / "attachments"
    hash_prefix = attachment_id[:2]  # "55"
    attachment_dir = storage_path / hash_prefix
    attachment_dir.mkdir(parents=True)

    # Create a simple test image using PIL (1x1 red pixel)
    test_image = Image.new("RGB", (1, 1), color="red")

    # Save to bytes
    image_buffer = BytesIO()
    test_image.save(image_buffer, format="PNG")
    test_image_content = image_buffer.getvalue()

    # Write to file
    attachment_file = attachment_dir / f"{attachment_id}.png"
    attachment_file.write_bytes(test_image_content)

    # Create and inject AttachmentService

    processing_service.attachment_service = AttachmentService(str(storage_path))

    # Create history with attachment URL
    history_messages = [
        {
            "role": "user",
            "content": "Check this image",
            "attachments": [
                {"type": "image", "content_url": f"/api/attachments/{attachment_id}"}
            ],
        },
    ]

    # Format the history
    actual_output = await processing_service._format_history_for_llm(history_messages)

    # Verify the attachment URL was converted to data URI
    assert len(actual_output) == 1
    assert actual_output[0]["role"] == "user"

    # Content should be a list with text and image parts
    content = actual_output[0]["content"]
    assert isinstance(content, list)
    assert len(content) == 2

    # First part should be text
    assert content[0]["type"] == "text"
    assert content[0]["text"] == "Check this image"

    # Second part should be converted image with data URI
    assert content[1]["type"] == "image_url"
    image_url = content[1]["image_url"]["url"]
    assert image_url.startswith("data:image/png;base64,")

    # Verify the base64 content matches
    expected_base64 = base64.b64encode(test_image_content).decode("utf-8")
    assert image_url == f"data:image/png;base64,{expected_base64}"


def test_web_specific_history_configuration() -> None:
    """Test that web interface gets different history limits than other interfaces."""
    # Create a service config with web-specific settings
    mock_service_config = ProcessingServiceConfig(
        prompts={},
        timezone_str="UTC",
        max_history_messages=5,  # Default for telegram
        history_max_age_hours=24,  # Default for telegram
        web_max_history_messages=100,  # Web-specific
        web_history_max_age_hours=720,  # Web-specific (30 days)
        tools_config={},
        delegation_security_level="confirm",
        id="test_web_history_profile",
    )

    processing_service = ProcessingService(
        llm_client=MockLLMClient(),
        tools_provider=MockToolsProvider(),
        service_config=mock_service_config,
        context_providers=[],
        server_url="http://test.com",
        app_config={},
    )

    # Test regular (non-web) history limits
    assert processing_service.max_history_messages == 5
    assert processing_service.history_max_age_hours == 24

    # Test web-specific history limits
    assert processing_service.web_max_history_messages == 100
    assert processing_service.web_history_max_age_hours == 720

    # Test that they're different
    assert (
        processing_service.max_history_messages
        != processing_service.web_max_history_messages
    )
    assert (
        processing_service.history_max_age_hours
        != processing_service.web_history_max_age_hours
    )


def test_web_history_configuration_fallback() -> None:
    """Test that web history configuration falls back to default when not specified."""
    # Create a service config WITHOUT web-specific settings
    mock_service_config = ProcessingServiceConfig(
        prompts={},
        timezone_str="UTC",
        max_history_messages=10,
        history_max_age_hours=48,
        web_max_history_messages=None,  # Not specified
        web_history_max_age_hours=None,  # Not specified
        tools_config={},
        delegation_security_level="confirm",
        id="test_fallback_profile",
    )

    processing_service = ProcessingService(
        llm_client=MockLLMClient(),
        tools_provider=MockToolsProvider(),
        service_config=mock_service_config,
        context_providers=[],
        server_url="http://test.com",
        app_config={},
    )

    # Test that web-specific properties fall back to default values
    assert (
        processing_service.web_max_history_messages == 10
    )  # Falls back to max_history_messages
    assert (
        processing_service.web_history_max_age_hours == 48
    )  # Falls back to history_max_age_hours


def test_web_history_configuration_with_zero_values() -> None:
    """Test that web history configuration correctly handles zero values."""
    # Create a service config with web-specific settings set to 0
    mock_service_config = ProcessingServiceConfig(
        prompts={},
        timezone_str="UTC",
        max_history_messages=10,
        history_max_age_hours=48,
        web_max_history_messages=0,  # Explicitly set to 0
        web_history_max_age_hours=0,  # Explicitly set to 0
        tools_config={},
        delegation_security_level="confirm",
        id="test_zero_values_profile",
    )

    processing_service = ProcessingService(
        llm_client=MockLLMClient(),
        tools_provider=MockToolsProvider(),
        service_config=mock_service_config,
        context_providers=[],
        server_url="http://test.com",
        app_config={},
    )

    # Test that zero values are respected, not treated as falsy and replaced with defaults
    assert processing_service.web_max_history_messages == 0  # Should be 0, not 10
    assert processing_service.web_history_max_age_hours == 0  # Should be 0, not 48
