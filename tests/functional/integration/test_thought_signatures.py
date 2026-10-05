"""Functional tests for thought signature round-trip through ProcessingService."""

from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any, TypeVar, cast
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolAttachment

from pydantic import BaseModel

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import (
    JsonObject,
    LLMMessage,
    LLMOutput,
    LLMStreamEvent,
    StreamEventMetadata,
    StructuredOutputError,
    ToolCallFunction,
    ToolCallItem,
    UserMessageDict,
)
from family_assistant.llm.google_types import (
    GeminiProviderMetadata,
    GeminiThoughtSignature,
)
from family_assistant.llm.messages import AssistantMessage, UserMessage
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.database import Database
from family_assistant.tools.types import ToolDefinition, ToolResult

T = TypeVar("T", bound=BaseModel)


def _thought_signature_bytes(provider_metadata: object) -> bytes | None:
    """Decode a tool call's thought signature from either form the Google client accepts."""
    if isinstance(provider_metadata, dict):
        provider_metadata = GeminiProviderMetadata.from_dict(
            cast("dict[str, Any]", provider_metadata)
        )
    if (
        isinstance(provider_metadata, GeminiProviderMetadata)
        and provider_metadata.thought_signature
    ):
        return provider_metadata.thought_signature.to_google_format()
    return None


class SimpleToolsProvider:
    """Minimal tools provider for testing."""

    async def get_tool_definitions(self) -> list:
        return [
            {
                "type": "function",
                "function": {
                    "name": "test_tool",
                    "description": "A test tool",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                        },
                    },
                },
            }
        ]

    async def execute_tool(
        self,
        name: str,
        arguments: dict,
        context: Any,  # noqa: ANN401 # Test mock uses Any
        call_id: str | None = None,
    ) -> str | ToolResult:
        return "Tool executed successfully"

    async def close(self) -> None:
        pass


class MockLLMWithThoughtSignatures:
    """Mock LLM client that simulates thought signatures in provider_metadata."""

    def __init__(self) -> None:
        self.messages_by_call: list[list[LLMMessage]] = []

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Generate mock response with thought signatures."""
        self.messages_by_call.append(list(messages))

        # First call: Return response with tool call and thought signature
        if len(self.messages_by_call) == 1:
            # Create a proper GeminiThoughtSignature object
            thought_sig = GeminiThoughtSignature(b"mock_thought_123")
            provider_metadata = GeminiProviderMetadata(thought_signature=thought_sig)

            return LLMOutput(
                content="I'll use the test tool",
                tool_calls=[
                    ToolCallItem(
                        id="call_1",
                        type="function",
                        function=ToolCallFunction(
                            name="test_tool",
                            arguments='{"query": "test"}',
                        ),
                        provider_metadata=provider_metadata,
                    )
                ],
            )

        # Second call: Return simple response (after tool execution)
        return LLMOutput(content="Tool executed successfully")

    def generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Streaming version - delegates to async generator."""
        return self._generate_response_stream(messages, tools, tool_choice)

    async def _generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Internal async generator for streaming."""
        # Use non-streaming generate_response and convert to stream events
        response = await self.generate_response(messages, tools, tool_choice)

        if response.content:
            yield LLMStreamEvent(type="content", content=response.content)

        if response.tool_calls:
            for tool_call in response.tool_calls:
                yield LLMStreamEvent(type="tool_call", tool_call=tool_call)

        metadata: StreamEventMetadata = {}
        if response.provider_metadata:
            metadata["provider_metadata"] = response.provider_metadata
        yield LLMStreamEvent(type="done", metadata=metadata)

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
    ) -> UserMessageDict:
        """Mock implementation - not needed for these tests."""
        return UserMessageDict(role="user", content=prompt_text or "")

    def create_attachment_injection(
        self,
        attachment: "ToolAttachment",
    ) -> "UserMessage":
        """Mock implementation - not needed for these tests."""
        return UserMessage(content="[System: File from previous tool response]")

    async def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
        max_retries: int = 2,
    ) -> T:
        """Mock implementation - raises error as not used in these tests."""
        raise StructuredOutputError(
            message="generate_structured not implemented in mock",
            provider="mock",
            model="mock",
        )

    async def generate_json(
        self,
        messages: Sequence[LLMMessage],
        max_retries: int = 2,
    ) -> JsonObject:
        """Mock implementation - raises error as not used in these tests."""
        raise StructuredOutputError(
            message="generate_json not implemented in mock",
            provider="mock",
            model="mock",
        )


class MockLLMWithThoughtSignaturesNoToolCalls:
    """Mock LLM client that returns thought signatures without tool calls."""

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Generate mock response with thought signature but no tool calls."""
        # Create a proper GeminiThoughtSignature object
        thought_sig = GeminiThoughtSignature(b"mock_thought_456")
        provider_metadata = GeminiProviderMetadata(thought_signature=thought_sig)

        return LLMOutput(
            content="Here's my response",
            provider_metadata=provider_metadata,
        )

    def generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Streaming version - delegates to async generator."""
        return self._generate_response_stream(messages, tools, tool_choice)

    async def _generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Internal async generator for streaming."""
        # Use non-streaming generate_response and convert to stream events
        response = await self.generate_response(messages, tools, tool_choice)

        if response.content:
            yield LLMStreamEvent(type="content", content=response.content)

        metadata: StreamEventMetadata = {}
        if response.provider_metadata:
            metadata["provider_metadata"] = response.provider_metadata
        yield LLMStreamEvent(type="done", metadata=metadata)

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
    ) -> UserMessageDict:
        """Mock implementation - not needed for these tests."""
        return UserMessageDict(role="user", content=prompt_text or "")

    def create_attachment_injection(
        self,
        attachment: "ToolAttachment",
    ) -> "UserMessage":
        """Mock implementation - not needed for these tests."""
        return UserMessage(content="[System: File from previous tool response]")

    async def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
        max_retries: int = 2,
    ) -> T:
        """Mock implementation - raises error as not used in these tests."""
        raise StructuredOutputError(
            message="generate_structured not implemented in mock",
            provider="mock",
            model="mock",
        )

    async def generate_json(
        self,
        messages: Sequence[LLMMessage],
        max_retries: int = 2,
    ) -> JsonObject:
        """Mock implementation - raises error as not used in these tests."""
        raise StructuredOutputError(
            message="generate_json not implemented in mock",
            provider="mock",
            model="mock",
        )


@pytest.mark.asyncio
async def test_thought_signatures_persist_and_roundtrip(
    db_engine: AsyncEngine,
) -> None:
    """Test that thought signatures are persisted to database and reconstructed on next call."""
    # Arrange: Create processing service with mock LLM that returns thought signatures
    mock_llm = MockLLMWithThoughtSignatures()
    config = ProcessingServiceConfig(
        prompts={"system_prompt": "You are a helpful assistant."},
        timezone=ZoneInfo("UTC"),
        history_budget_chars=100_000,
        history_max_age_hours=24,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.CONFIRM,
        id="test_profile",
    )
    processing_service = ProcessingService(
        llm_client=mock_llm,
        tools_provider=SimpleToolsProvider(),
        service_config=config,
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
    )

    # Act: Process first message
    db_context = Database(db_engine)
    result = await processing_service.handle_chat_interaction(
        db_context=db_context,
        interface_type="test",
        conversation_id="test_conv_123",
        trigger_content_parts=[{"type": "text", "text": "Use test tool"}],
        trigger_interface_message_id="msg_1",
        user_name="Test User",
    )

    # Assert: Verify thought signature was stored in database
    assert result.assistant_message_internal_id is not None

    # Retrieve the stored message from database
    stored_messages = await db_context.message_history.get_recent(
        interface_type="test",
        conversation_id="test_conv_123",
        limit=10,
    )

    # Find the assistant message with tool calls
    assistant_msg = next(
        (msg for msg in stored_messages if msg.role == "assistant" and msg.tool_calls),
        None,
    )
    assert assistant_msg is not None
    assert isinstance(assistant_msg.provider_metadata, GeminiProviderMetadata)
    assert assistant_msg.provider_metadata.thought_signature is not None
    thought_sig = assistant_msg.provider_metadata.thought_signature
    assert thought_sig.to_google_format() == b"mock_thought_123"

    # Assert: the follow-up call after the tool ran hands the signature back to
    # the model on the tool call it belongs to, which is where the Google
    # client reads it from when building the request.
    assert len(mock_llm.messages_by_call) == 2
    replayed_tool_calls = {
        tool_call.id: tool_call
        for message in mock_llm.messages_by_call[1]
        if isinstance(message, AssistantMessage) and message.tool_calls
        for tool_call in message.tool_calls
    }
    assert "call_1" in replayed_tool_calls
    assert (
        _thought_signature_bytes(replayed_tool_calls["call_1"].provider_metadata)
        == b"mock_thought_123"
    )


@pytest.mark.asyncio
async def test_thought_signatures_without_tool_calls(
    db_engine: AsyncEngine,
) -> None:
    """Test that thought signatures are preserved for responses without tool calls."""
    # Arrange: Create processing service with mock LLM
    mock_llm = MockLLMWithThoughtSignaturesNoToolCalls()
    config = ProcessingServiceConfig(
        prompts={"system_prompt": "You are a helpful assistant."},
        timezone=ZoneInfo("UTC"),
        history_budget_chars=100_000,
        history_max_age_hours=24,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.CONFIRM,
        id="test_profile",
    )
    processing_service = ProcessingService(
        llm_client=mock_llm,
        tools_provider=SimpleToolsProvider(),
        service_config=config,
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
    )

    # Act: Process message
    db_context = Database(db_engine)
    result = await processing_service.handle_chat_interaction(
        db_context=db_context,
        interface_type="test",
        conversation_id="test_conv_456",
        trigger_content_parts=[{"type": "text", "text": "Simple question"}],
        trigger_interface_message_id="msg_2",
        user_name="Test User",
    )

    # Assert: Verify thought signature was stored even without tool calls
    assert result.assistant_message_internal_id is not None

    # Retrieve the stored message
    stored_messages = await db_context.message_history.get_recent(
        interface_type="test",
        conversation_id="test_conv_456",
        limit=10,
    )

    # Find the assistant message
    assistant_msg = next(
        (msg for msg in stored_messages if msg.role == "assistant"),
        None,
    )
    assert assistant_msg is not None
    assert assistant_msg.provider_metadata is not None
    # provider_metadata is now a GeminiProviderMetadata object, not a dict
    assert isinstance(assistant_msg.provider_metadata, GeminiProviderMetadata)
    assert assistant_msg.provider_metadata.thought_signature is not None

    # Verify signature content
    thought_sig = assistant_msg.provider_metadata.thought_signature
    # GeminiThoughtSignature stores raw bytes, use to_google_format() to retrieve
    assert thought_sig.to_google_format() == b"mock_thought_456"
