"""Unit tests for large tool-result handling in AttachmentProcessor."""

from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from family_assistant.config_models import AppConfig
from family_assistant.processing.attachments import AttachmentProcessor
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.utils.clock import SystemClock


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
async def test_handle_large_result_without_registry_returns_explicit_error() -> None:
    """Large content should not fall back to inline text when storage is unavailable."""
    processor = _create_processor(attachment_registry=None)
    oversized_content = "A" * 4096

    new_content, attachment_id = await processor.handle_large_result(
        db_context=Mock(),
        content=oversized_content,
        tool_name="test_tool",
        conversation_id="conv_1",
        call_id="call_1",
    )

    assert attachment_id is None
    assert "too large to inline" in new_content
    assert len(new_content) < len(oversized_content)


@pytest.mark.asyncio
async def test_handle_large_result_storage_failure_returns_explicit_error() -> None:
    """Attachment storage failures should return an explicit error message."""
    mock_registry = Mock()
    mock_registry.store_and_register_tool_attachment = AsyncMock(
        side_effect=RuntimeError("disk full")
    )
    processor = _create_processor(
        attachment_registry=cast("AttachmentRegistry", mock_registry)
    )
    oversized_content = "B" * 4096

    new_content, attachment_id = await processor.handle_large_result(
        db_context=Mock(),
        content=oversized_content,
        tool_name="test_tool",
        conversation_id="conv_2",
        call_id="call_2",
    )

    assert attachment_id is None
    assert (
        new_content
        == "Error: Tool result from 'test_tool' was too large and could not be stored as an attachment."
    )
    mock_registry.store_and_register_tool_attachment.assert_awaited_once()


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
