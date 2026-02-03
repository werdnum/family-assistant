"""Tests for large tool result auto-attachment and reading tools."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.attachments import read_text_attachment_tool
from family_assistant.tools.execute_script import execute_script_tool
from family_assistant.tools.types import (
    ToolExecutionContext,
)
from family_assistant.utils.clock import SystemClock


@pytest.mark.asyncio
async def test_large_tool_result_auto_attachment(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Test that large tool results are automatically converted to attachments."""
    # Setup AttachmentRegistry
    storage_path = tmp_path / "attachments"
    storage_path.mkdir()
    attachment_registry = AttachmentRegistry(
        storage_path=str(storage_path),
        db_engine=db_engine,
        config={
            "max_file_size": 100 * 1024 * 1024,
            "max_multimodal_size": 20 * 1024 * 1024,
        },
    )

    # Setup ProcessingService
    config = ProcessingServiceConfig(
        prompts={},
        timezone_str="UTC",
        max_history_messages=10,
        history_max_age_hours=1.0,
        tools_config={},
        delegation_security_level="unrestricted",
        id="test-profile",
    )

    mock_llm = Mock()
    mock_tools_provider = AsyncMock()

    # Mock AppConfig for attachment threshold
    mock_app_config = Mock()
    mock_app_config.attachment_selection_threshold = 5
    mock_app_config.attachment_config = Mock()
    mock_app_config.attachment_config.large_tool_result_threshold_kb = 20

    service = ProcessingService(
        llm_client=mock_llm,
        tools_provider=mock_tools_provider,
        service_config=config,
        context_providers=[],
        server_url="http://localhost:8000",
        app_config=mock_app_config,
        attachment_registry=attachment_registry,
        clock=SystemClock(),
    )

    async with DatabaseContext(engine=db_engine) as db:
        # 1. Test large string result
        large_content = "A" * (20 * 1024 + 100)
        mock_tools_provider.execute_tool.return_value = large_content

        tool_call = ToolCallItem(
            id="call_1",
            type="function",
            function=ToolCallFunction(name="large_tool", arguments={}),
        )

        result = await service._execute_single_tool(
            tool_call,
            interface_type="test",
            conversation_id="conv_1",
            user_name="test_user",
            turn_id="turn_1",
            db_context=db,
            chat_interface=None,
        )

        assert result.llm_message.content is not None
        assert "was too large and was saved as attachment" in result.llm_message.content
        assert "read_text_attachment" in result.llm_message.content
        assert result.auto_attachment_ids is not None
        assert len(result.auto_attachment_ids) == 1
        att_id = result.auto_attachment_ids[0]

        # Verify attachment content
        saved_content = await attachment_registry.get_attachment_content(db, att_id)
        assert saved_content is not None
        assert saved_content.decode("utf-8") == large_content

        # 2. Test large JSON result
        large_json_obj = {"data": "B" * (20 * 1024)}
        large_json_str = json.dumps(large_json_obj)
        mock_tools_provider.execute_tool.return_value = large_json_str

        tool_call = ToolCallItem(
            id="call_2",
            type="function",
            function=ToolCallFunction(name="json_tool", arguments={}),
        )

        result = await service._execute_single_tool(
            tool_call,
            interface_type="test",
            conversation_id="conv_1",
            user_name="test_user",
            turn_id="turn_2",
            db_context=db,
            chat_interface=None,
        )

        assert result.llm_message.content is not None
        assert "jq_query" in result.llm_message.content
        assert result.auto_attachment_ids is not None
        att_id = result.auto_attachment_ids[0]
        metadata = await attachment_registry.get_attachment(db, att_id)
        assert metadata is not None
        assert metadata.mime_type == "application/json"


@pytest.mark.asyncio
async def test_read_text_attachment_tool(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Test the read_text_attachment tool functionality."""
    storage_path = tmp_path / "attachments"
    storage_path.mkdir()
    attachment_registry = AttachmentRegistry(
        storage_path=str(storage_path), db_engine=db_engine, config=None
    )

    async with DatabaseContext(engine=db_engine) as db:
        # Create a text attachment manually
        text_content = "Line 1\nLine 2\nTarget Line\nLine 4"
        reg_metadata = await attachment_registry.store_and_register_tool_attachment(
            file_content=text_content.encode("utf-8"),
            filename="test.txt",
            content_type="text/plain",
            tool_name="test",
            description="test",
            conversation_id="conv_1",
        )
        text_att_id = reg_metadata.attachment_id

        exec_ctx = ToolExecutionContext(
            interface_type="test",
            conversation_id="conv_1",
            user_name="test",
            turn_id=None,
            db_context=db,
            processing_service=None,
            clock=SystemClock(),
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=attachment_registry,
            camera_backend=None,
        )

        # Test grep
        read_result = await read_text_attachment_tool(
            exec_ctx, text_att_id, grep="Target"
        )
        assert read_result.text is not None
        assert "Target Line" in read_result.text
        assert "Line 1" not in read_result.text

        # Test offset/limit
        read_result = await read_text_attachment_tool(
            exec_ctx, text_att_id, offset=1, limit=1
        )
        assert read_result.text is not None
        assert "Line 2" in read_result.text
        assert "Line 1" not in read_result.text
        assert "Target Line" not in read_result.text


@pytest.mark.asyncio
async def test_starlark_attachment_read(db_engine: AsyncEngine, tmp_path: Path) -> None:
    """Test reading attachment content from Starlark script."""
    storage_path = tmp_path / "attachments"
    storage_path.mkdir()
    attachment_registry = AttachmentRegistry(
        storage_path=str(storage_path), db_engine=db_engine, config=None
    )

    async with DatabaseContext(engine=db_engine) as db:
        # Create a text attachment
        text_content = "Hello from Starlark attachment!"
        reg_metadata = await attachment_registry.store_and_register_tool_attachment(
            file_content=text_content.encode("utf-8"),
            filename="hello.txt",
            content_type="text/plain",
            tool_name="test",
            description="test",
            conversation_id="conv_script",
        )
        att_id = reg_metadata.attachment_id

        exec_ctx = ToolExecutionContext(
            interface_type="test",
            conversation_id="conv_script",
            user_name="test",
            turn_id=None,
            db_context=db,
            processing_service=None,
            clock=SystemClock(),
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=attachment_registry,
            camera_backend=None,
        )

        script = f"""
content = attachment_read('{att_id}')
content
"""
        result = await execute_script_tool(exec_ctx, script)
        assert result.text is not None
        assert text_content in result.text
