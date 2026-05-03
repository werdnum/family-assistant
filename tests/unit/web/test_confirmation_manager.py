"""Tests for web confirmation lifecycle tracking."""

from __future__ import annotations

import asyncio

import pytest

from family_assistant.web.confirmation_manager import WebConfirmationManager


@pytest.mark.asyncio
async def test_web_confirmation_decision_future_is_separate_from_execution_future() -> (
    None
):
    """Approving a web confirmation resolves only the local user-decision wait."""
    manager = WebConfirmationManager()
    execution_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    decision_future = await manager.request_confirmation(
        request_id="confirm_test",
        conversation_id="conversation",
        interface_type="web",
        tool_name="test_tool",
        tool_args={"value": "test"},
        confirmation_prompt="Run test_tool?",
        timeout_seconds=0.1,
    )

    assert not decision_future.done()
    assert not execution_future.done()

    assert manager.resolve_approved("confirm_test")
    decision_outcome = await decision_future

    assert decision_outcome.kind == "approved"
    assert not execution_future.done()

    manager.remove_confirmation("confirm_test")
    assert manager.pending_confirmations == {}
