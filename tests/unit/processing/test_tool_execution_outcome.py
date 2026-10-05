"""A tool's structured failure must reach the stream and history as a failure."""

from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.processing.attachments import AttachmentProcessor
from family_assistant.processing.tool_execution import ToolExecutor
from family_assistant.processing.types import ProcessingServiceConfig
from family_assistant.tools.outcomes import classify_tool_outcome
from family_assistant.tools.types import ToolResult
from family_assistant.utils.clock import SystemClock


async def _execute(result: ToolResult) -> tuple[str | None, str | None, str]:
    tools_provider = AsyncMock()
    tools_provider.execute_tool.return_value = result
    executor = ToolExecutor(
        tools_provider=tools_provider,
        config=ProcessingServiceConfig(
            id="test",
            prompts={},
            timezone=ZoneInfo("UTC"),
            history_budget_chars=100_000,
            history_max_age_hours=24,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
        ),
        attachment_processor=AttachmentProcessor(
            attachment_registry=None, app_config=AppConfig(), clock=SystemClock()
        ),
        attachment_registry=None,
        clock=SystemClock(),
        credential_resolvers=None,
        api_backend=None,
    )
    output = await executor.execute(
        tool_call_item_obj=ToolCallItem(
            id="call-1",
            type="function",
            function=ToolCallFunction(name="download_media", arguments="{}"),
        ),
        interface_type="test",
        conversation_id="conv",
        user_name="tester",
        turn_id="turn",
        db_context=Mock(),
        chat_interface=None,
        request_confirmation_callback=None,
    )
    return (
        output.stream_event.error,
        output.llm_message.error_traceback,
        output.stream_event.tool_result or "",
    )


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_structured_error_data_marks_the_call_failed() -> None:
    reason = "timed out"
    stream_error, row_error, text = await _execute(
        ToolResult(text=f"Download failed: {reason}", data={"error": reason})
    )

    assert text == "Download failed: timed out"
    assert classify_tool_outcome(text, stream_error) == "failed"
    assert classify_tool_outcome(text, row_error) == "failed"


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_structured_success_data_stays_succeeded() -> None:
    path = "clip.mp4"
    stream_error, row_error, text = await _execute(
        ToolResult(text=f"Downloaded {path}", data={"path": path})
    )

    assert stream_error is None
    assert row_error is None
    assert classify_tool_outcome(text, row_error) == "succeeded"
