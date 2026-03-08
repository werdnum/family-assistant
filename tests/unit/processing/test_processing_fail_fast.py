"""Fail-fast and parity behavior tests for the processing module."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.llm import (
    LLMOutput,
    LLMStreamEvent,
    ToolCallFunction,
    ToolCallItem,
)
from family_assistant.llm.messages import AssistantMessage, ErrorMessage
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.processing.attachments import AttachmentProcessor
from family_assistant.processing.context import ContextPreparer
from family_assistant.storage.context import get_db_context
from family_assistant.utils.clock import SystemClock
from tests.mocks.mock_llm import (  # pylint: disable=no-name-in-module
    RuleBasedMockLLMClient,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.llm.messages import ContentPartDict
    from family_assistant.tools import ToolExecutionContext
    from family_assistant.tools.types import ToolDefinition, ToolResult


class SimpleToolsProvider:
    async def get_tool_definitions(self) -> list[ToolDefinition]:
        return []

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - tool args are dynamic
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        return ""

    async def close(self) -> None:
        pass


def _make_service(
    llm_client: RuleBasedMockLLMClient | None = None,
    max_iterations: int = 5,
) -> ProcessingService:
    config = ProcessingServiceConfig(
        prompts={"system_prompt": "You are a helper. {current_time}"},
        timezone=ZoneInfo("UTC"),
        max_history_messages=10,
        history_max_age_hours=24,
        tools_config=ToolsConfig(enable_local_tools=[], enable_mcp_server_ids=[]),
        delegation_security_level="confirm",
        id="processing_fail_fast_test",
        max_iterations=max_iterations,
    )
    return ProcessingService(
        llm_client=llm_client
        or RuleBasedMockLLMClient(
            rules=[],
            default_response=LLMOutput(content="ok"),
        ),
        tools_provider=SimpleToolsProvider(),
        service_config=config,
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
    )


@pytest.mark.asyncio
async def test_sync_forwards_chat_interfaces_to_process_message(
    db_engine: AsyncEngine,
) -> None:
    service = _make_service()
    process_message_mock = AsyncMock(return_value=([], None, None))
    service.process_message = process_message_mock  # type: ignore[method-assign]

    async with get_db_context(db_engine) as db_context:
        await service.handle_chat_interaction(
            db_context=db_context,
            interface_type="test",
            conversation_id="conv_sync_forwarding",
            trigger_content_parts=[{"type": "text", "text": "hello"}],
            trigger_interface_message_id="msg-1",
            user_name="tester",
            chat_interfaces={"web": MagicMock()},
        )

    assert process_message_mock.await_args is not None
    captured_kwargs = process_message_mock.await_args.kwargs
    assert "chat_interfaces" in captured_kwargs
    assert isinstance(captured_kwargs["chat_interfaces"], dict)
    assert "web" in captured_kwargs["chat_interfaces"]


@pytest.mark.asyncio
async def test_stream_forwards_user_and_subconversation_to_process_message_stream(
    db_engine: AsyncEngine,
) -> None:
    service = _make_service()
    captured_kwargs: dict[str, object] = {}

    async def fake_process_message_stream(
        **kwargs: object,
    ) -> AsyncIterator[tuple[LLMStreamEvent, AssistantMessage | None]]:
        captured_kwargs.update(kwargs)
        yield (
            LLMStreamEvent(type="done", metadata={}),
            AssistantMessage(content="stream response"),
        )

    service.process_message_stream = fake_process_message_stream  # type: ignore[method-assign]

    async with get_db_context(db_engine) as db_context:
        events: list[LLMStreamEvent] = []
        async for event in service.handle_chat_interaction_stream(
            db_context=db_context,
            interface_type="test",
            conversation_id="conv_stream_forwarding",
            trigger_content_parts=[{"type": "text", "text": "hello"}],
            trigger_interface_message_id="msg-2",
            user_name="tester",
            user_id="user-123",
            subconversation_id="sub-abc",
        ):
            events.append(event)

    assert events
    assert captured_kwargs.get("user_id") == "user-123"
    assert captured_kwargs.get("subconversation_id") == "sub-abc"


@pytest.mark.asyncio
async def test_sync_persists_errors_as_error_messages(db_engine: AsyncEngine) -> None:
    service = _make_service()
    service.process_message = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

    async with get_db_context(db_engine) as db_context:
        result = await service.handle_chat_interaction(
            db_context=db_context,
            interface_type="test",
            conversation_id="conv_sync_error_message",
            trigger_content_parts=[{"type": "text", "text": "hello"}],
            trigger_interface_message_id="msg-3",
            user_name="tester",
        )

        assert result.has_error

        saved_messages = await db_context.message_history.get_recent(
            interface_type="test",
            conversation_id="conv_sync_error_message",
            limit=10,
            max_age=timedelta(hours=24),
            processing_profile_id=service.service_config.id,
            subconversation_id=None,
            current_time=service.clock.now(),
        )
        assert any(isinstance(message, ErrorMessage) for message in saved_messages)


@pytest.mark.asyncio
async def test_stream_persists_errors_as_error_messages(db_engine: AsyncEngine) -> None:
    service = _make_service()

    async def fake_process_message_stream(
        **kwargs: object,
    ) -> AsyncIterator[tuple[LLMStreamEvent, AssistantMessage | None]]:
        if False:  # pragma: no cover - keeps this as an async generator
            yield (LLMStreamEvent(type="done"), None)
        raise RuntimeError("stream boom")

    service.process_message_stream = fake_process_message_stream  # type: ignore[method-assign]

    async with get_db_context(db_engine) as db_context:
        events: list[LLMStreamEvent] = []
        async for event in service.handle_chat_interaction_stream(
            db_context=db_context,
            interface_type="test",
            conversation_id="conv_stream_error_message",
            trigger_content_parts=[{"type": "text", "text": "hello"}],
            trigger_interface_message_id="msg-4",
            user_name="tester",
        ):
            events.append(event)

        assert any(event.type == "error" for event in events)

        saved_messages = await db_context.message_history.get_recent(
            interface_type="test",
            conversation_id="conv_stream_error_message",
            limit=10,
            max_age=timedelta(hours=24),
            processing_profile_id=service.service_config.id,
            subconversation_id=None,
            current_time=service.clock.now(),
        )
        assert any(isinstance(message, ErrorMessage) for message in saved_messages)


@pytest.mark.no_db
def test_llm_loop_infers_attachment_types_from_mime_type() -> None:
    service = _make_service()

    assert service.llm_loop._infer_attachment_type("image/png") == "image"  # noqa: SLF001
    assert service.llm_loop._infer_attachment_type("video/mp4") == "video"  # noqa: SLF001
    assert service.llm_loop._infer_attachment_type("audio/mpeg") == "audio"  # noqa: SLF001
    assert service.llm_loop._infer_attachment_type("application/pdf") == "document"  # noqa: SLF001
    assert service.llm_loop._infer_attachment_type("text/plain") == "file"  # noqa: SLF001
    assert service.llm_loop._infer_attachment_type(None) == "file"  # noqa: SLF001


@pytest.mark.asyncio
async def test_final_iteration_tool_calls_do_not_raise_processing_error(
    db_engine: AsyncEngine,
) -> None:
    tool_call = ToolCallItem(
        id="call_final_iteration",
        type="function",
        function=ToolCallFunction(name="example_tool", arguments='{"x": 1}'),
    )
    llm_client = RuleBasedMockLLMClient(
        rules=[],
        default_response=LLMOutput(content=None, tool_calls=[tool_call]),
    )
    service = _make_service(llm_client=llm_client, max_iterations=1)

    async with get_db_context(db_engine) as db_context:
        result = await service.handle_chat_interaction(
            db_context=db_context,
            interface_type="test",
            conversation_id="conv_final_iteration_tool_calls",
            trigger_content_parts=[{"type": "text", "text": "hello"}],
            trigger_interface_message_id="msg-final-1",
            user_name="tester",
        )

    assert not result.has_error


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_context_aggregate_context_raises_on_provider_failure() -> None:
    class BrokenProvider:
        name = "broken-provider"

        async def get_context_fragments(self) -> list[str]:
            raise RuntimeError("provider failed")

    config = ProcessingServiceConfig(
        prompts={},
        timezone=ZoneInfo("UTC"),
        max_history_messages=10,
        history_max_age_hours=24,
        tools_config=ToolsConfig(),
        delegation_security_level="confirm",
        id="context-fail-fast",
    )
    preparer = ContextPreparer(
        context_providers=[BrokenProvider()],
        service_config=config,
        clock=SystemClock(),
    )

    with pytest.raises(
        RuntimeError,
        match="Context provider 'broken-provider' failed to provide fragments",
    ):
        await preparer.aggregate_context()


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_process_content_parts_missing_attachment_id_raises() -> None:
    processor = AttachmentProcessor(
        attachment_registry=AsyncMock(),
        llm_client=MagicMock(),
        app_config=AppConfig(),
        clock=SystemClock(),
    )

    with pytest.raises(ValueError, match="missing required 'attachment_id'"):
        await processor.process_content_parts(
            db_context=MagicMock(),
            conversation_id="conv",
            content_parts=cast("list[ContentPartDict]", [{"type": "attachment"}]),
        )


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_process_content_parts_missing_attachment_metadata_raises() -> None:
    mock_registry = AsyncMock()
    mock_registry.get_attachment.return_value = None
    processor = AttachmentProcessor(
        attachment_registry=mock_registry,
        llm_client=MagicMock(),
        app_config=AppConfig(),
        clock=SystemClock(),
    )

    with pytest.raises(ValueError, match="was not found in attachment registry"):
        await processor.process_content_parts(
            db_context=MagicMock(),
            conversation_id="conv",
            content_parts=cast(
                "list[ContentPartDict]",
                [{"type": "attachment", "attachment_id": "att-1"}],
            ),
        )


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_process_content_parts_missing_attachment_content_raises() -> None:
    mock_registry = AsyncMock()
    mock_registry.get_attachment.return_value = MagicMock(mime_type="text/plain")
    mock_registry.get_attachment_content.return_value = None
    processor = AttachmentProcessor(
        attachment_registry=mock_registry,
        llm_client=MagicMock(),
        app_config=AppConfig(),
        clock=SystemClock(),
    )

    with pytest.raises(RuntimeError, match="content could not be retrieved"):
        await processor.process_content_parts(
            db_context=MagicMock(),
            conversation_id="conv",
            content_parts=cast(
                "list[ContentPartDict]",
                [{"type": "attachment", "attachment_id": "att-1"}],
            ),
        )


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_convert_urls_to_data_uris_invalid_internal_url_raises() -> None:
    mock_registry = MagicMock()
    processor = AttachmentProcessor(
        attachment_registry=mock_registry,
        llm_client=MagicMock(),
        app_config=AppConfig(),
        clock=SystemClock(),
    )

    with pytest.raises(ValueError, match="Invalid attachment URL format"):
        await processor.convert_urls_to_data_uris([
            {"type": "image_url", "image_url": {"url": "/api/attachments/not-a-uuid"}}
        ])


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_convert_urls_to_data_uris_missing_file_raises() -> None:
    mock_registry = MagicMock()
    mock_registry.get_attachment_path.return_value = Path(
        "/tmp/file-does-not-exist.png"
    )
    processor = AttachmentProcessor(
        attachment_registry=mock_registry,
        llm_client=MagicMock(),
        app_config=AppConfig(),
        clock=SystemClock(),
    )

    with pytest.raises(FileNotFoundError, match="Attachment file not found"):
        await processor.convert_urls_to_data_uris([
            {
                "type": "image_url",
                "image_url": {
                    "url": "/api/attachments/550e8400-e29b-41d4-a716-446655440000"
                },
            }
        ])
