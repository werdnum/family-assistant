"""
Unit tests for the history formatting logic in ProcessingService.
"""

import json
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.tools.types import ToolDefinition

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.content_parts import attachment_content
from family_assistant.llm.messages import (
    AssistantMessage,
    LLMMessage,
    SystemMessage,
    TextContentPart,
    ToolMessage,
    UserMessage,
)
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.security.taint import (
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage import message_history_table
from family_assistant.storage.database import Database
from family_assistant.tools.types import ToolExecutionContext
from tests.factories.messages import (
    create_assistant_message,
    create_error_message,
    create_tool_call,
    create_tool_message,
    create_user_message,
)
from tests.mocks.mock_llm import LLMOutput, MatcherArgs, RuleBasedMockLLMClient


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
        # ast-grep-ignore: no-dict-any - tool arguments from external LLM tool call
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
        timezone=ZoneInfo("UTC"),  # Not used
        max_history_messages=10,  # Not used
        history_max_age_hours=1,  # Not used
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.CONFIRM,  # Added
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
    actual_output = await processing_service.context_preparer.format_history(
        history_messages
    )
    assert actual_output == expected_output


async def test_format_history_preserves_assistant_taint(
    processing_service: ProcessingService,
) -> None:
    taint_metadata = (
        TurnTaintState
        .empty()
        .add_source(
            TaintSource(
                source_type=TaintSourceType.TOOL_OUTPUT,
                source_id="voice-session",
                tier=SourceTrustTier.UNKNOWN_EXTERNAL,
                labels=frozenset(),
                reason="voice transcript includes tool output",
            )
        )
        .to_metadata()
    )

    actual_output = await processing_service.context_preparer.format_history([
        AssistantMessage(content="Voice reply", taint_metadata=taint_metadata)
    ])

    assert actual_output == [
        AssistantMessage(content="Voice reply", taint_metadata=taint_metadata)
    ]


async def test_handle_chat_interaction_persists_system_trigger(
    db_engine: "AsyncEngine",
) -> None:
    """System-triggered turns are durable history, not hidden model-only context."""
    seen_messages: list[LLMMessage] = []

    def response_generator(args: MatcherArgs) -> LLMOutput:
        seen_messages.extend(args["messages"])
        return LLMOutput(content="Relayed delegated result.", tool_calls=None)

    service = ProcessingService(
        llm_client=RuleBasedMockLLMClient(
            rules=[(lambda _args: True, response_generator)]
        ),
        tools_provider=MockToolsProvider(),
        service_config=ProcessingServiceConfig(
            prompts={},
            timezone=ZoneInfo("UTC"),
            max_history_messages=10,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
            id="system_trigger_profile",
        ),
        context_providers=[],
        server_url="http://test.com",
        app_config=AppConfig(),
    )

    db_context = Database(engine=db_engine)
    result = await service.handle_chat_interaction(
        db_context=db_context,
        interface_type="web",
        conversation_id="system-trigger-conversation",
        trigger_content_parts=[
            {
                "type": "text",
                "text": "System: Delegated profile task completed.",
            }
        ],
        trigger_interface_message_id=None,
        user_name="Test User",
        trigger_role="system",
        trigger_attachments=[
            {
                "type": "attachment_reference",
                "attachment_id": "delegated-attachment-1",
                "filename": "delegated-output.txt",
            }
        ],
    )

    rows = await db_context.fetch_all(
        message_history_table
        .select()
        .where(message_history_table.c.conversation_id == "system-trigger-conversation")
        .order_by(message_history_table.c.internal_id)
    )
    future_history = await db_context.message_history.get_recent(
        interface_type="web",
        conversation_id="system-trigger-conversation",
        limit=10,
        processing_profile_id="system_trigger_profile",
        current_time=service.clock.now(),
    )

    assert result.text_reply == "Relayed delegated result."
    assert len(rows) == 2
    assert rows[0]["role"] == "system"
    assert rows[0]["content"] == "System: Delegated profile task completed."
    assert rows[0]["attachments"] == [
        {
            "type": "attachment_reference",
            "attachment_id": "delegated-attachment-1",
            "filename": "delegated-output.txt",
        }
    ]
    assert rows[0]["processing_profile_id"] == "system_trigger_profile"
    assert rows[1]["role"] == "assistant"
    assert any(
        isinstance(message, SystemMessage)
        and "System: Delegated profile task completed." in message.content
        and "delegated-attachment-1" in message.content
        and "<attachment_metadata>" in message.content
        for message in seen_messages
    )
    assert not any(
        isinstance(message, SystemMessage)
        and "System: Delegated profile task completed." in message.content
        for message in future_history
    )
    assert any(
        isinstance(message, UserMessage)
        and isinstance(message.content, str)
        and "Historical delegation completion event from a previous turn."
        in message.content
        and "System: Delegated profile task completed." in message.content
        for message in future_history
    )
    assert not any(
        isinstance(message, UserMessage)
        and isinstance(message.content, str)
        and "Respond to the user with the result." in message.content
        for message in future_history
    )


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

    actual_output = await processing_service.context_preparer.format_history(
        history_messages
    )
    assert actual_output == expected_output


async def test_format_history_strips_assistant_text_for_tool_call_without_signature(
    processing_service: ProcessingService,
) -> None:
    """Assistant text is stripped when tool calls are present without thought signatures."""
    tool_call = create_tool_call(
        call_id="call_no_signature",
        function_name="get_weather",
        arguments='{"location": "London"}',
    )
    history_messages: list[LLMMessage] = [
        create_assistant_message(
            content="I will call a tool now",
            tool_calls=[tool_call],
        ),
    ]

    actual_output = await processing_service.context_preparer.format_history(
        history_messages
    )

    assert actual_output == [
        AssistantMessage(content=None, tool_calls=[tool_call]),
    ]


async def test_format_history_preserves_assistant_text_with_thought_signature(
    processing_service: ProcessingService,
) -> None:
    """Assistant text is preserved when tool calls contain Google thought signatures."""
    tool_call = ToolCallItem(
        id="call_with_signature",
        type="function",
        function=ToolCallFunction(
            name="get_weather",
            arguments='{"location": "London"}',
        ),
        provider_metadata={
            "provider": "google",
            "thought_signature": "abc123",
        },
    )
    history_messages: list[LLMMessage] = [
        create_assistant_message(
            content="I will call a tool now",
            tool_calls=[tool_call],
        ),
    ]

    actual_output = await processing_service.context_preparer.format_history(
        history_messages
    )

    assert actual_output == [
        AssistantMessage(
            content="I will call a tool now",
            tool_calls=[tool_call],
        ),
    ]


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

    actual_output = await processing_service.context_preparer.format_history(
        history_messages
    )
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
    actual_output = await processing_service.context_preparer.format_history(
        history_messages
    )
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
    actual_output = await processing_service.context_preparer.format_history(
        history_messages
    )
    assert actual_output == expected_output


async def test_delegated_attachments_are_named_to_the_model(
    db_engine: "AsyncEngine", tmp_path: Path
) -> None:
    """Attachments handed over by delegation reach the model with their ids.

    A delegated profile is given the bytes as an injection message. Without the
    id alongside them it can look at the image but cannot pass it to any
    attachment-taking tool, so it invents a substitute instead.
    """
    seen_messages: list[LLMMessage] = []

    def response_generator(args: MatcherArgs) -> LLMOutput:
        seen_messages.extend(args["messages"])
        return LLMOutput(content="Transformed the image.", tool_calls=None)

    service = ProcessingService(
        llm_client=RuleBasedMockLLMClient(
            rules=[(lambda _args: True, response_generator)]
        ),
        tools_provider=MockToolsProvider(),
        service_config=ProcessingServiceConfig(
            prompts={},
            timezone=ZoneInfo("UTC"),
            max_history_messages=10,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
            id="artist_profile",
        ),
        context_providers=[],
        server_url="http://test.com",
        app_config=AppConfig(),
    )
    service.attachment_registry = AttachmentRegistry(
        storage_path=str(tmp_path / "attachments"),
        db_engine=db_engine,
        config=None,
    )

    image_buffer = BytesIO()
    Image.new("RGB", (1, 1), color="red").save(image_buffer, format="JPEG")
    db_context = Database(engine=db_engine)
    attachment = await service.attachment_registry.register_user_attachment(
        db_context=db_context,
        content=image_buffer.getvalue(),
        filename="backyard.jpeg",
        mime_type="image/jpeg",
        conversation_id="delegated-conversation",
        user_id="user-1",
    )

    await service.handle_chat_interaction(
        db_context=db_context,
        interface_type="web",
        conversation_id="delegated-conversation",
        trigger_content_parts=[
            {"type": "text", "text": "Mock up the hill with the proposed plants"},
            attachment_content(attachment.attachment_id),
        ],
        trigger_interface_message_id=None,
        user_name="Test User",
        user_id="user-1",
    )

    metadata_blocks = [
        message
        for message in seen_messages
        if isinstance(message, UserMessage)
        and "<attachment_metadata>" in _message_text(message)
    ]
    assert len(metadata_blocks) == 1
    block_text = _message_text(metadata_blocks[0])
    assert attachment.attachment_id in block_text
    assert "backyard.jpeg" in block_text
    assert "image/jpeg" in block_text
    # On the request itself, not on an injection message: a provider adapter
    # may put an injection's payload in `parts`, and then only `parts` is sent.
    assert "Mock up the hill with the proposed plants" in block_text
    assert metadata_blocks[0].parts is None


def _message_text(message: UserMessage) -> str:
    """Flatten a user message's text, whether it is a string or content parts."""
    if isinstance(message.content, str):
        return message.content
    return "\n".join(
        part.text for part in message.content if isinstance(part, TextContentPart)
    )


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
    actual_output = await processing_service.context_preparer.format_history(
        history_messages
    )

    # Verify the message is preserved correctly as a typed message
    assert len(actual_output) == 1
    assert isinstance(actual_output[0], UserMessage)
    assert actual_output[0].content == "Check this image"


def test_web_specific_history_configuration() -> None:
    """Test that web interface gets different history limits than other interfaces."""
    mock_service_config = ProcessingServiceConfig(
        prompts={},
        timezone=ZoneInfo("UTC"),
        max_history_messages=5,
        history_max_age_hours=24,
        web_max_history_messages=100,
        web_history_max_age_hours=720,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.CONFIRM,
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

    # Test regular (non-web) history limits via context_preparer
    non_web_limit, non_web_age = processing_service.context_preparer.get_history_limits(
        "telegram"
    )
    assert non_web_limit == 5
    assert non_web_age == timedelta(hours=24)

    # Test web-specific history limits via context_preparer
    web_limit, web_age = processing_service.context_preparer.get_history_limits("web")
    assert web_limit == 100
    assert web_age == timedelta(hours=720)

    # Test that they're different
    assert non_web_limit != web_limit
    assert non_web_age != web_age


def test_web_history_configuration_fallback() -> None:
    """Test that web history configuration falls back to default when not specified."""
    mock_service_config = ProcessingServiceConfig(
        prompts={},
        timezone=ZoneInfo("UTC"),
        max_history_messages=10,
        history_max_age_hours=48,
        web_max_history_messages=None,
        web_history_max_age_hours=None,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.CONFIRM,
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

    # Test that web limits fall back to default values via context_preparer
    web_limit, web_age = processing_service.context_preparer.get_history_limits("web")
    assert web_limit == 10
    assert web_age == timedelta(hours=48)

    # Confirm the raw config values are None (no web-specific override)
    assert processing_service.service_config.web_max_history_messages is None
    assert processing_service.service_config.web_history_max_age_hours is None


def test_web_history_configuration_with_zero_values() -> None:
    """Test that web history configuration correctly handles zero values."""
    mock_service_config = ProcessingServiceConfig(
        prompts={},
        timezone=ZoneInfo("UTC"),
        max_history_messages=10,
        history_max_age_hours=48,
        web_max_history_messages=0,
        web_history_max_age_hours=0,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.CONFIRM,
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

    # Test that zero values are respected (not treated as falsy) via context_preparer
    web_limit, web_age = processing_service.context_preparer.get_history_limits("web")
    assert web_limit == 0
    assert web_age == timedelta(hours=0)
