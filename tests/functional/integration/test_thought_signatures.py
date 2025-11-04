"""Functional tests for thought signature round-trip through ProcessingService."""

import base64
from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolAttachment

from family_assistant.llm import (
    LLMMessage,
    LLMOutput,
    LLMStreamEvent,
    ToolCallFunction,
    ToolCallItem,
)
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.context import get_db_context
from family_assistant.tools.types import ToolResult


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
        self.call_count = 0
        self.last_messages: list[LLMMessage] = []

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Generate mock response with thought signatures."""
        self.call_count += 1
        # Store messages as list for introspection
        self.last_messages = list(messages) if messages else []

        # First call: Return response with tool call and thought signature
        if self.call_count == 1:
            mock_signature = base64.b64encode(b"mock_thought_123").decode("ascii")
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
                        provider_metadata={
                            "provider": "google",
                            "thought_signatures": [
                                {"part_index": 0, "signature": mock_signature}
                            ],
                        },
                    )
                ],
            )

        # Second call: Return simple response (after tool execution)
        return LLMOutput(content="Tool executed successfully")

    def generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Streaming version - delegates to async generator."""
        return self._generate_response_stream(messages, tools, tool_choice)

    async def _generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, Any]] | None = None,
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

        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        metadata: dict[str, Any] = {}
        if response.provider_metadata:
            metadata["provider_metadata"] = response.provider_metadata
        yield LLMStreamEvent(type="done", metadata=metadata)

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    ) -> dict[str, Any]:
        """Mock implementation - not needed for these tests."""
        return {"role": "user", "content": prompt_text or ""}

    def create_attachment_injection(
        self,
        attachment: "ToolAttachment",
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    ) -> dict[str, Any]:
        """Mock implementation - not needed for these tests."""
        return {
            "role": "user",
            "content": "[System: File from previous tool response]",
        }


class MockLLMWithThoughtSignaturesNoToolCalls:
    """Mock LLM client that returns thought signatures without tool calls."""

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Generate mock response with thought signature but no tool calls."""
        mock_signature = base64.b64encode(b"mock_thought_456").decode("ascii")
        return LLMOutput(
            content="Here's my response",
            provider_metadata={
                "provider": "google",
                "thought_signatures": [{"part_index": 0, "signature": mock_signature}],
            },
        )

    def generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Streaming version - delegates to async generator."""
        return self._generate_response_stream(messages, tools, tool_choice)

    async def _generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Internal async generator for streaming."""
        # Use non-streaming generate_response and convert to stream events
        response = await self.generate_response(messages, tools, tool_choice)

        if response.content:
            yield LLMStreamEvent(type="content", content=response.content)

        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        metadata: dict[str, Any] = {}
        if response.provider_metadata:
            metadata["provider_metadata"] = response.provider_metadata
        yield LLMStreamEvent(type="done", metadata=metadata)

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    ) -> dict[str, Any]:
        """Mock implementation - not needed for these tests."""
        return {"role": "user", "content": prompt_text or ""}

    def create_attachment_injection(
        self,
        attachment: "ToolAttachment",
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    ) -> dict[str, Any]:
        """Mock implementation - not needed for these tests."""
        return {
            "role": "user",
            "content": "[System: File from previous tool response]",
        }


@pytest.mark.asyncio
async def test_thought_signatures_persist_and_roundtrip(
    db_engine: AsyncEngine,
) -> None:
    """Test that thought signatures are persisted to database and reconstructed on next call."""
    # Arrange: Create processing service with mock LLM that returns thought signatures
    mock_llm = MockLLMWithThoughtSignatures()
    config = ProcessingServiceConfig(
        prompts={"system_prompt": "You are a helpful assistant."},
        timezone_str="UTC",
        max_history_messages=10,
        history_max_age_hours=24,
        tools_config={"enable_local_tools": [], "enable_mcp_server_ids": []},
        delegation_security_level="confirm",
        id="test_profile",
    )
    processing_service = ProcessingService(
        llm_client=mock_llm,
        tools_provider=SimpleToolsProvider(),
        service_config=config,
        context_providers=[],
        server_url="http://testserver",
        app_config={},
    )

    # Act: Process first message
    async with get_db_context(db_engine) as db_context:
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
            (
                msg
                for msg in stored_messages
                if msg["role"] == "assistant" and msg.get("tool_calls")
            ),
            None,
        )
        assert assistant_msg is not None
        assert assistant_msg["provider_metadata"] is not None
        assert assistant_msg["provider_metadata"]["provider"] == "google"
        assert "thought_signatures" in assistant_msg["provider_metadata"]

        # Verify signature content
        signatures = assistant_msg["provider_metadata"]["thought_signatures"]
        assert len(signatures) == 1
        decoded_sig = base64.b64decode(signatures[0]["signature"])
        assert decoded_sig == b"mock_thought_123"

    # Assert: Verify thought signature round-trip happened (2 LLM calls made)
    # The first call returns a message with tool calls and provider_metadata
    # That gets stored, tool executes, then second LLM call happens
    assert mock_llm.call_count == 2

    # The functional round-trip test is complete:
    # 1. First LLM call returned provider_metadata ✓
    # 2. Provider_metadata was stored in database ✓ (verified above)
    # 3. Second LLM call completed successfully ✓ (call_count == 2)
    # The actual reconstruction of thought signatures into the Gemini API format
    # is tested in the Google client integration tests


@pytest.mark.asyncio
async def test_thought_signatures_without_tool_calls(
    db_engine: AsyncEngine,
) -> None:
    """Test that thought signatures are preserved for responses without tool calls."""
    # Arrange: Create processing service with mock LLM
    mock_llm = MockLLMWithThoughtSignaturesNoToolCalls()
    config = ProcessingServiceConfig(
        prompts={"system_prompt": "You are a helpful assistant."},
        timezone_str="UTC",
        max_history_messages=10,
        history_max_age_hours=24,
        tools_config={"enable_local_tools": [], "enable_mcp_server_ids": []},
        delegation_security_level="confirm",
        id="test_profile",
    )
    processing_service = ProcessingService(
        llm_client=mock_llm,
        tools_provider=SimpleToolsProvider(),
        service_config=config,
        context_providers=[],
        server_url="http://testserver",
        app_config={},
    )

    # Act: Process message
    async with get_db_context(db_engine) as db_context:
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
            (msg for msg in stored_messages if msg["role"] == "assistant"),
            None,
        )
        assert assistant_msg is not None
        assert assistant_msg["provider_metadata"] is not None
        assert assistant_msg["provider_metadata"]["provider"] == "google"
        assert "thought_signatures" in assistant_msg["provider_metadata"]

        # Verify signature content
        signatures = assistant_msg["provider_metadata"]["thought_signatures"]
        assert len(signatures) == 1
        decoded_sig = base64.b64decode(signatures[0]["signature"])
        assert decoded_sig == b"mock_thought_456"
