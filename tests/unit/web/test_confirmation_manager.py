"""Tests for web confirmation lifecycle tracking."""

from __future__ import annotations

import pytest

from family_assistant.web.confirmation_manager import WebConfirmationManager


@pytest.mark.asyncio
async def test_resolve_approved_resolves_decision_future_with_approved() -> None:
    """Approving a web confirmation resolves the decision future exactly once."""
    manager = WebConfirmationManager()

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

    assert manager.resolve_approved("confirm_test")
    decision_outcome = await decision_future

    assert decision_outcome.kind == "approved"

    assert not manager.resolve_approved("confirm_test")
    assert not manager.resolve_rejected("confirm_test")

    manager.remove_confirmation("confirm_test")
    assert manager.pending_confirmations == {}
