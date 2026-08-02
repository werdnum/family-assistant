"""Unit tests for large tool-result handling in AttachmentProcessor."""

from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.processing.attachments import AttachmentProcessor
from family_assistant.processing.tool_execution import ToolExecutor
from family_assistant.processing.types import ProcessingServiceConfig
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database
from family_assistant.tools.types import ToolAttachment, ToolResult
from family_assistant.utils.clock import SystemClock

if TYPE_CHECKING:
    from family_assistant.security.taint import TaintMetadata


def _create_processor(
    attachment_registry: AttachmentRegistry | None,
    threshold_kb: int = 1,
) -> AttachmentProcessor:
    app_config = AppConfig()
    app_config.attachment_config.large_tool_result_threshold_kb = threshold_kb
    return AttachmentProcessor(
        attachment_registry=attachment_registry,
        llm_client=Mock(),
        app_config=app_config,
        clock=SystemClock(),
    )


@pytest.mark.asyncio
async def test_handle_large_result_without_registry_raises() -> None:
    """Large content should fail fast when storage is unavailable."""
    processor = _create_processor(attachment_registry=None)
    oversized_content = "A" * 4096

    with pytest.raises(
        RuntimeError,
        match="Tool result exceeded large-result threshold but attachment storage is unavailable",
    ):
        await processor.handle_large_result(
            db_context=Mock(),
            content=oversized_content,
            tool_name="test_tool",
            conversation_id="conv_1",
            call_id="call_1",
        )


@pytest.mark.asyncio
async def test_handle_large_result_storage_failure_raises() -> None:
    """Attachment storage failures should propagate."""
    mock_registry = Mock()
    mock_registry.store_and_register_tool_attachment = AsyncMock(
        side_effect=RuntimeError("disk full")
    )
    processor = _create_processor(
        attachment_registry=cast("AttachmentRegistry", mock_registry)
    )
    oversized_content = "B" * 4096

    with pytest.raises(RuntimeError, match="disk full"):
        await processor.handle_large_result(
            db_context=Mock(),
            content=oversized_content,
            tool_name="test_tool",
            conversation_id="conv_2",
            call_id="call_2",
        )

    mock_registry.store_and_register_tool_attachment.assert_awaited_once()


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_tool_executor_propagates_large_result_registry_unavailable() -> None:
    """ToolExecutor should fail fast when large-result conversion cannot persist."""
    mock_tools_provider = AsyncMock()
    mock_tools_provider.execute_tool.return_value = "X" * 4096
    processor = _create_processor(attachment_registry=None, threshold_kb=1)
    executor = ToolExecutor(
        tools_provider=mock_tools_provider,
        config=ProcessingServiceConfig(
            id="test",
            prompts={},
            timezone=ZoneInfo("UTC"),
            max_history_messages=10,
            history_max_age_hours=24,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
        ),
        attachment_processor=processor,
        attachment_registry=None,
        clock=SystemClock(),
        credential_resolvers=None,
        api_backend=None,
    )

    with pytest.raises(
        RuntimeError,
        match="Tool result exceeded large-result threshold but attachment storage is unavailable",
    ):
        await executor.execute(
            tool_call_item_obj=ToolCallItem(
                id="call-1",
                type="function",
                function=ToolCallFunction(name="test_tool", arguments="{}"),
            ),
            interface_type="test",
            conversation_id="conv",
            user_name="tester",
            turn_id="turn",
            db_context=Mock(),
            chat_interface=None,
            request_confirmation_callback=None,
        )

    mock_tools_provider.execute_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_large_result_marks_json_content_type_without_full_parse() -> None:
    """Large JSON-shaped output should be stored with application/json MIME type."""
    mock_registry = Mock()
    mock_registry.store_and_register_tool_attachment = AsyncMock(
        return_value=Mock(attachment_id="att_json_1")
    )
    processor = _create_processor(
        attachment_registry=cast("AttachmentRegistry", mock_registry)
    )
    large_json_payload = '{"data": "' + ("X" * 4096) + '"}'

    new_content, attachment_id = await processor.handle_large_result(
        db_context=Mock(),
        content=large_json_payload,
        tool_name="test_tool",
        conversation_id="conv_json",
        call_id="call_json",
    )

    assert attachment_id == "att_json_1"
    assert "saved as attachment" in new_content
    call_kwargs = mock_registry.store_and_register_tool_attachment.await_args.kwargs
    assert call_kwargs["content_type"] == "application/json"


@pytest.mark.asyncio
async def test_handle_large_result_keeps_text_plain_for_non_json_shape() -> None:
    """Large plain text output should be stored with text/plain MIME type."""
    mock_registry = Mock()
    mock_registry.store_and_register_tool_attachment = AsyncMock(
        return_value=Mock(attachment_id="att_txt_1")
    )
    processor = _create_processor(
        attachment_registry=cast("AttachmentRegistry", mock_registry)
    )
    large_text_payload = "not-json-" + ("Y" * 4096)

    _, attachment_id = await processor.handle_large_result(
        db_context=Mock(),
        content=large_text_payload,
        tool_name="test_tool",
        conversation_id="conv_text",
        call_id="call_text",
    )

    assert attachment_id == "att_txt_1"
    call_kwargs = mock_registry.store_and_register_tool_attachment.await_args.kwargs
    assert call_kwargs["content_type"] == "text/plain"


@pytest.mark.asyncio
async def test_handle_large_result_persists_taint_metadata() -> None:
    """Auto-converted large results should carry result taint in attachment metadata."""
    mock_registry = Mock()
    mock_registry.store_and_register_tool_attachment = AsyncMock(
        return_value=Mock(attachment_id="att_taint_1")
    )
    processor = _create_processor(
        attachment_registry=cast("AttachmentRegistry", mock_registry)
    )
    taint_metadata = cast(
        "TaintMetadata",
        {
            "version": "runtime_v1",
            "max_tier": "unknown_external",
            "sources": [],
        },
    )

    _, attachment_id = await processor.handle_large_result(
        db_context=Mock(),
        content="tainted-" + ("Z" * 4096),
        tool_name="test_tool",
        conversation_id="conv_taint",
        call_id="call_taint",
        taint_metadata=taint_metadata,
    )

    assert attachment_id == "att_taint_1"
    call_kwargs = mock_registry.store_and_register_tool_attachment.await_args.kwargs
    assert call_kwargs["metadata"]["taint_metadata"] == taint_metadata


@pytest.mark.asyncio
async def test_tool_result_attachment_registration_persists_taint_metadata() -> None:
    """Explicit ToolResult attachments should carry result taint in registry metadata."""
    mock_registry = Mock()
    mock_registry.store_and_register_tool_attachment = AsyncMock(
        return_value=Mock(
            attachment_id="att_explicit_1",
            content_url="memory://att_explicit_1",
        )
    )
    processor = _create_processor(
        attachment_registry=cast("AttachmentRegistry", mock_registry),
        threshold_kb=100,
    )
    executor = ToolExecutor(
        tools_provider=AsyncMock(),
        config=ProcessingServiceConfig(
            id="test",
            prompts={},
            timezone=ZoneInfo("UTC"),
            max_history_messages=10,
            history_max_age_hours=24,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
        ),
        attachment_processor=processor,
        attachment_registry=cast("AttachmentRegistry", mock_registry),
        clock=SystemClock(),
        credential_resolvers=None,
        api_backend=None,
    )
    taint_metadata = cast(
        "TaintMetadata",
        {
            "version": "runtime_v1",
            "max_tier": "unknown_external",
            "sources": [],
        },
    )

    (
        _,
        llm_message,
        stream_metadata,
        attachment_ids,
    ) = await executor._build_output_for_tool_result(
        db_context=Mock(),
        result=ToolResult(
            text="small result",
            attachments=[
                ToolAttachment(
                    content=b"tainted attachment",
                    mime_type="text/plain",
                    description="external text",
                )
            ],
        ),
        function_name="test_tool",
        conversation_id="conv_explicit",
        call_id="call_explicit",
        provider_metadata=None,
        taint_metadata=taint_metadata,
        acting_user_id=None,
        arguments=None,
    )

    assert attachment_ids == ["att_explicit_1"]
    assert llm_message.taint_metadata == taint_metadata
    assert stream_metadata == {
        "attachments": [
            {
                "type": "tool_result",
                "mime_type": "text/plain",
                "description": "external text",
                "content_url": "memory://att_explicit_1",
                "attachment_id": "att_explicit_1",
            }
        ]
    }
    call_kwargs = mock_registry.store_and_register_tool_attachment.await_args.kwargs
    assert call_kwargs["metadata"]["taint_metadata"] == taint_metadata


@pytest.mark.asyncio
async def test_large_result_inherits_ownership_from_owned_argument_attachment(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """Derived large results keep the owner of an owned input attachment.

    A helper like jq_query run over an owner-scoped Gmail attachment must not
    launder its content into an ownerless attachment.
    """
    registry = AttachmentRegistry(
        storage_path=str(tmp_path), db_engine=db_engine, config=None
    )
    processor = _create_processor(attachment_registry=registry, threshold_kb=1)
    executor = ToolExecutor(
        tools_provider=AsyncMock(),
        config=ProcessingServiceConfig(
            id="test",
            prompts={},
            timezone=ZoneInfo("UTC"),
            max_history_messages=10,
            history_max_age_hours=24,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
        ),
        attachment_processor=processor,
        attachment_registry=registry,
        clock=SystemClock(),
        credential_resolvers=None,
        api_backend=None,
    )

    db = Database(engine=db_engine)
    owned = await registry.store_and_register_tool_attachment(
        file_content=b"owned gmail bytes",
        filename="owned.txt",
        content_type="text/plain",
        tool_name="gmail_get_attachment",
        owner_user_id="user-a",
        db_context=db,
    )

    (
        _,
        derived_ids,
    ) = await executor._handle_large_text_result(  # exercising the internal ownership hook directly
        db_context=db,
        content="D" * 4096,
        function_name="jq_query",
        conversation_id="conv-derived",
        call_id="call-derived",
        taint_metadata=None,
        acting_user_id="user-a",
        arguments={"attachment_id": owned.attachment_id, "query": "."},
    )
    assert len(derived_ids) == 1
    derived = await registry.get_attachment(db, derived_ids[0], acting_user_id="user-a")
    assert derived is not None
    assert derived.owner_user_id == "user-a"
    assert (
        await registry.get_attachment(db, derived_ids[0], acting_user_id=None) is None
    )

    (
        _,
        plain_ids,
    ) = await executor._handle_large_text_result(  # exercising the internal ownership hook directly
        db_context=db,
        content="E" * 4096,
        function_name="jq_query",
        conversation_id="conv-derived",
        call_id="call-plain",
        taint_metadata=None,
        acting_user_id="user-a",
        arguments={"query": "."},
    )
    assert len(plain_ids) == 1
    plain = await registry.get_attachment(db, plain_ids[0], acting_user_id=None)
    assert plain is not None
    assert plain.owner_user_id is None
