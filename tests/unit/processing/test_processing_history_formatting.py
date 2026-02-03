"""
Unit tests for the history formatting logic in ProcessingService.
"""

import json
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from PIL import Image

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolDefinition

from family_assistant.config_models import AppConfig
from family_assistant.llm.messages import (
    AssistantMessage,
    LLMMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.tools.types import ToolExecutionContext
from tests.factories.messages import (
    create_assistant_message,
    create_error_message,
    create_tool_call,
    create_tool_message,
    create_user_message,
)
from tests.mocks.mock_llm import RuleBasedMockLLMClient


class MockToolsProvider:
    async def get_tool_definitions(
        self,
        *args: Any,  # noqa: ANN401  # Mock needs flexibility
        **kwargs: Any,  # noqa: ANN401 # Mock needs flexibility
    ) -> "list[ToolDefinition]":
        return []  # Not used

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
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
        llm_client=RuleBasedMockLLMClient(rules=[]),
        tools_provider=MockToolsProvider(),
        service_config=mock_service_config,  # Use the config object
        context_providers=[],
        server_url="http://test.com",  # Not used
        app_config=AppConfig(),  # Add dummy app_config
    )


# --- Test Cases ---


async def test_format_simple_history(processing_service: ProcessingService) -> None:
    """Test formatting a simple user-assistant conversation."""
    history_messages = [
        create_user_message("Hello"),
        create_assistant_message("Hi there!"),
    ]
    expected_output = [
        UserMessage(content="Hello"),
        AssistantMessage(content="Hi there!"),
    ]
    actual_output = await processing_service._format_history_for_llm(history_messages)
    assert actual_output == expected_output


async def test_format_history_with_tool_call(
    processing_service: ProcessingService,
) -> None:
    """Test formatting history including an assistant message with a tool call."""
    tool_call_id = "call_123"
    tool_name = "get_weather"
    tool_args = {"location": "London"}
    tool_response = "Weather in London is sunny."
    tool_call = create_tool_call(
        call_id=tool_call_id,
        function_name=tool_name,
        arguments=json.dumps(tool_args),
    )

    history_messages = [
        create_user_message("What's the weather like?"),
        create_assistant_message(
            content=None,
            tool_calls=[tool_call],
        ),
        create_tool_message(
            tool_call_id=tool_call_id,
            content=tool_response,
            name=tool_name,
        ),
    ]

    expected_output = [
        UserMessage(content="What's the weather like?"),
        AssistantMessage(tool_calls=[tool_call]),
        ToolMessage(
            tool_call_id=tool_call_id,
            content=tool_response,
            name=tool_name,
        ),
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
    tool_call_2 = create_tool_call(
        call_id=tool_call_id2,
        function_name=tool_name2,
        arguments=json.dumps(tool_args2),
    )

    history_messages = [
        create_tool_message(
            tool_call_id=tool_call_id1,
            content="Response from first tool",
            name="some_tool",
        ),
        create_assistant_message(
            content=None,
            tool_calls=[tool_call_2],
        ),
        create_user_message("Follow-up user message"),
    ]

    expected_output = [
        ToolMessage(
            tool_call_id=tool_call_id1,
            content="Response from first tool",
            name="some_tool",
        ),
        AssistantMessage(tool_calls=[tool_call_2]),
        UserMessage(content="Follow-up user message"),
    ]

    actual_output = await processing_service._format_history_for_llm(history_messages)
    assert actual_output == expected_output


async def test_format_history_includes_errors_as_assistant(
    processing_service: ProcessingService,
) -> None:
    """Test that messages with role 'error' are included as assistant messages."""
    history_messages = [
        create_user_message("Try something"),
        create_error_message(
            content="Something went wrong",
            error_traceback="Traceback...",
        ),
        create_assistant_message("Okay"),
    ]
    expected_output = [
        UserMessage(content="Try something"),
        AssistantMessage(
            content="I encountered an error: Something went wrong\n\nError details: Traceback..."
        ),
        AssistantMessage(content="Okay"),
    ]
    actual_output = await processing_service._format_history_for_llm(history_messages)
    assert actual_output == expected_output


async def test_format_history_handles_empty_tool_calls(
    processing_service: ProcessingService,
) -> None:
    """Test formatting an assistant message where tool_calls_info is explicitly empty."""
    history_messages = [
        create_user_message("User message"),
        create_assistant_message(
            content="Assistant message",
            tool_calls=[],
        ),
    ]
    expected_output = [
        UserMessage(content="User message"),
        AssistantMessage(content="Assistant message", tool_calls=[]),
    ]
    actual_output = await processing_service._format_history_for_llm(history_messages)
    assert actual_output == expected_output


async def test_format_history_converts_attachment_urls(
    processing_service: ProcessingService, tmp_path: Path
) -> None:
    """Test that _format_history_for_llm preserves message structure correctly."""

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

    # Create a mock db_engine for AttachmentRegistry

    mock_db_engine = MagicMock()

    processing_service.attachment_registry = AttachmentRegistry(
        storage_path=str(storage_path),
        db_engine=mock_db_engine,
        config=None,
    )

    # Create history with user message
    history_messages: list[LLMMessage] = [
        create_user_message("Check this image"),
    ]

    # Format the history
    actual_output = await processing_service._format_history_for_llm(history_messages)

    # Verify the message is preserved correctly as a typed message
    assert len(actual_output) == 1
    assert isinstance(actual_output[0], UserMessage)
    assert actual_output[0].content == "Check this image"


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
        llm_client=RuleBasedMockLLMClient(rules=[]),
        tools_provider=MockToolsProvider(),
        service_config=mock_service_config,
        context_providers=[],
        server_url="http://test.com",
        app_config=AppConfig(),
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
        llm_client=RuleBasedMockLLMClient(rules=[]),
        tools_provider=MockToolsProvider(),
        service_config=mock_service_config,
        context_providers=[],
        server_url="http://test.com",
        app_config=AppConfig(),
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
        llm_client=RuleBasedMockLLMClient(rules=[]),
        tools_provider=MockToolsProvider(),
        service_config=mock_service_config,
        context_providers=[],
        server_url="http://test.com",
        app_config=AppConfig(),
    )

    # Test that zero values are respected, not treated as falsy and replaced with defaults
    assert processing_service.web_max_history_messages == 0  # Should be 0, not 10
    assert processing_service.web_history_max_age_hours == 0  # Should be 0, not 48
