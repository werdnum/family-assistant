"""Unit tests for the delegate_to_service confirmation renderer.

The confirmation prompt is the sole safeguard before a profile (notably the
read-only engineer) hands its context to another profile, so the user must be
able to review the full delegated request, its target, and any attachments —
and be told explicitly when content is truncated rather than silently shown a
short prefix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from family_assistant.tools.confirmation import (
    CONFIRMATION_VALUE_MAX_CHARS,
    render_delegate_to_service_confirmation,
)

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext


def _no_context() -> ToolExecutionContext:
    # The renderer ignores its context argument.
    return cast("ToolExecutionContext", None)


@pytest.mark.asyncio
async def test_delegate_confirmation_shows_target_request_and_attachments() -> None:
    prompt = await render_delegate_to_service_confirmation(
        {
            "target_service_id": "default_assistant",
            "user_request": "Please apply the fix in services.py",
            "attachment_ids": ["att-1", "att-2"],
        },
        _no_context(),
    )

    assert "default_assistant" in prompt
    assert "Please apply the fix in services.py" in prompt
    assert "att-1" in prompt
    assert "att-2" in prompt
    assert "[truncated]" not in prompt


@pytest.mark.asyncio
async def test_delegate_confirmation_marks_truncated_request() -> None:
    long_request = "x" * (CONFIRMATION_VALUE_MAX_CHARS + 500)

    prompt = await render_delegate_to_service_confirmation(
        {"target_service_id": "complex_tasks", "user_request": long_request},
        _no_context(),
    )

    # The user is shown a bounded slice and explicitly told it was truncated,
    # so a long request cannot hide sensitive content behind a short prefix.
    assert "... [truncated]" in prompt
    assert long_request not in prompt
