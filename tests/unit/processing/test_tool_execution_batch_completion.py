"""The executor reports a call's completion to its batch however it ends.

Browser operations wait for their earlier siblings in the batch, so a call that
is denied, fails, or never reaches its tool must still report — otherwise it
wedges the rest of the response's tool calls.
"""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest

from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.storage.database import Database
from family_assistant.tools import ToolPolicyDeniedError
from family_assistant.tools.types import ToolCallBatch, ToolResult

from .test_tool_execution_safety_confirmation import (
    MinimalToolsProvider,
    make_tool_executor,
)

pytestmark = pytest.mark.asyncio


def _tool_call(call_id: str, name: str = "browser_click") -> ToolCallItem:
    return ToolCallItem(
        id=call_id,
        type="function",
        function=ToolCallFunction(name=name, arguments="{}"),
    )


def _batch() -> ToolCallBatch:
    return ToolCallBatch([
        ("call_1", "browser_click"),
        ("call_2", "browser_fill"),
    ])


async def _run(
    provider: MinimalToolsProvider, batch: ToolCallBatch, call_id: str
) -> None:
    executor = make_tool_executor(provider)
    await executor.execute(
        _tool_call(call_id),
        interface_type="test",
        conversation_id="conv_batch",
        user_name="testuser",
        turn_id="turn_1",
        db_context=Mock(spec=Database),
        chat_interface=None,
        request_confirmation_callback=None,
        tool_call_batch=batch,
    )


async def test_successful_call_reports_completion() -> None:
    batch = _batch()
    await _run(
        MinimalToolsProvider(result=ToolResult(data={"ok": True})), batch, "call_1"
    )
    await asyncio.wait_for(batch.wait_done(["call_1"]), timeout=1)


async def test_denied_call_reports_completion() -> None:
    """A policy denial returns an error result; siblings must not wait on it."""
    batch = _batch()
    provider = MinimalToolsProvider(
        error=ToolPolicyDeniedError("browser_click", "denied by policy")
    )
    await _run(provider, batch, "call_1")
    await asyncio.wait_for(batch.wait_done(["call_1"]), timeout=1)


async def test_failing_call_reports_completion() -> None:
    batch = _batch()
    provider = MinimalToolsProvider(error=RuntimeError("boom"))
    await _run(provider, batch, "call_1")
    await asyncio.wait_for(batch.wait_done(["call_1"]), timeout=1)


async def test_malformed_arguments_report_completion() -> None:
    """The call is rejected before it reaches the tool, and still reports."""
    batch = _batch()
    executor = make_tool_executor(MinimalToolsProvider())
    await executor.execute(
        ToolCallItem(
            id="call_1",
            type="function",
            function=ToolCallFunction(name="browser_click", arguments="not json"),
        ),
        interface_type="test",
        conversation_id="conv_batch",
        user_name="testuser",
        turn_id="turn_1",
        db_context=Mock(spec=Database),
        chat_interface=None,
        request_confirmation_callback=None,
        tool_call_batch=batch,
    )
    await asyncio.wait_for(batch.wait_done(["call_1"]), timeout=1)


async def test_call_without_a_batch_still_executes() -> None:
    provider = MinimalToolsProvider(result=ToolResult(data={"ok": True}))
    executor = make_tool_executor(provider)
    result = await executor.execute(
        _tool_call("call_solo"),
        interface_type="test",
        conversation_id="conv_batch",
        user_name="testuser",
        turn_id="turn_1",
        db_context=Mock(spec=Database),
        chat_interface=None,
        request_confirmation_callback=None,
    )
    assert result.llm_message.content
    assert provider.executed_tool_names == ["browser_click"]
