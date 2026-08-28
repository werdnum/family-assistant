"""Tests for deferred confirmation callback classification."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast
from unittest.mock import Mock

import pytest

from family_assistant.security.taint import TurnTaintState
from family_assistant.services.deferred_tool_confirmation import (
    DeferredConfirmationCallbackAdapter,
)
from family_assistant.tools.types import ConfirmationOutcome

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolArguments, ToolExecutionContext


class _OutcomeCallback:
    def __init__(self, outcome: ConfirmationOutcome) -> None:
        self.outcome = outcome

    async def __call__(
        self,
        interface_type: str,
        conversation_id: str,
        turn_id: str | None,
        tool_name: str,
        call_id: str,
        tool_args: ToolArguments,
        timeout_seconds: float,
        context: ToolExecutionContext,
    ) -> ConfirmationOutcome:
        del (
            interface_type,
            conversation_id,
            turn_id,
            tool_name,
            call_id,
            tool_args,
            timeout_seconds,
            context,
        )
        return self.outcome


async def _invoke_adapter(outcome: ConfirmationOutcome) -> ConfirmationOutcome:
    callback = _OutcomeCallback(outcome)
    adapter = DeferredConfirmationCallbackAdapter(callback)
    return await adapter(
        "api",
        "conversation",
        "turn",
        "reviewed_tool",
        "call",
        {},
        60,
        cast("ToolExecutionContext", Mock()),
    )


@pytest.mark.no_db
async def test_deferred_adapter_marks_completed_placeholder_not_attempted() -> None:
    taint_metadata = TurnTaintState.empty().to_metadata()
    result = await _invoke_adapter(
        ConfirmationOutcome(
            kind="completed",
            result="Approval request queued; the tool has not run.",
            taint_metadata=taint_metadata,
        )
    )

    assert result == ConfirmationOutcome(
        kind="completed",
        result="Approval request queued; the tool has not run.",
        action_attempted=False,
        taint_metadata=taint_metadata,
    )


@pytest.mark.no_db
@pytest.mark.parametrize(
    "kind",
    ["approved", "rejected", "timed_out", "cancelled", "failed"],
)
async def test_deferred_adapter_preserves_noncompleted_outcomes(
    kind: Literal["approved", "rejected", "timed_out", "cancelled", "failed"],
) -> None:
    outcome = ConfirmationOutcome(kind=kind, result=f"{kind} result")

    result = await _invoke_adapter(outcome)

    assert result is outcome
