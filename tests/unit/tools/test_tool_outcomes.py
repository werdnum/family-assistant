"""Unit tests for classifying a finished tool call's outcome."""

from __future__ import annotations

import json
from typing import Literal

import pytest

from family_assistant.tools.infrastructure import confirmation_outcome_to_tool_result
from family_assistant.tools.outcomes import ToolOutcome, classify_tool_outcome
from family_assistant.tools.types import ConfirmationOutcome


@pytest.mark.parametrize(
    ("content", "error_traceback", "expected"),
    [
        ("Note 'Groceries' saved.", None, "succeeded"),
        ("", None, "succeeded"),
        ('{"notes": []}', None, "succeeded"),
        ('{"success": true, "id": 3}', None, "succeeded"),
        ("Errors found: none", None, "succeeded"),
        ("Error: Database temporarily unavailable", None, "failed"),
        ("  Error executing add_or_update_note: boom", None, "failed"),
        ("Error during confirmation process for tool 'x': boom", None, "failed"),
        ("Error executing add_or_update_note: boom", "Traceback ...", "failed"),
        ("anything at all", "", "failed"),
        ('{"error": "Document 7 not found"}', None, "failed"),
        ('{"success": false, "message": "no such callback"}', None, "failed"),
        ('{"status": "error", "message": "bad"}', None, "failed"),
        ('{"status": "failed", "site": "bank"}', None, "failed"),
        ('{"status": "refused", "reason": "policy"}', None, "failed"),
        (
            "Waiting on the user to approve this in Telegram or the web UI "
            "(request 7). It hasn't run yet.",
            None,
            "rejected",
        ),
        (
            "Action blocked by automatic review for tool 'delete_note': unsafe",
            None,
            "rejected",
        ),
        ("OK. Action cancelled by user: delegation to service 'x'.", None, "rejected"),
        ('{"error": null, "rows": 2}', None, "succeeded"),
        ("[1, 2]", None, "succeeded"),
        (None, None, "succeeded"),
        ([{"type": "text", "text": "Error: inside a list"}], None, "succeeded"),
    ],
)
def test_classify_tool_outcome(
    content: object, error_traceback: str | None, expected: ToolOutcome
) -> None:
    assert classify_tool_outcome(content, error_traceback) == expected


@pytest.mark.parametrize("kind", ["rejected", "timed_out", "cancelled"])
def test_confirmation_gate_results_are_rejected(
    kind: Literal["rejected", "timed_out", "cancelled"],
) -> None:
    result = confirmation_outcome_to_tool_result(
        name="delete_note",
        outcome=ConfirmationOutcome(kind=kind),
    )
    assert isinstance(result, str)
    assert classify_tool_outcome(result, None) == "rejected"
    # The executor records a gate timeout with an empty traceback; it is still
    # a call that never ran, not a failure.
    assert classify_tool_outcome(result, "") == "rejected"


def test_failed_approved_execution_is_failed() -> None:
    content = json.dumps({"error": "boom", "safety_acknowledgement": True})
    assert classify_tool_outcome(content, None) == "failed"
