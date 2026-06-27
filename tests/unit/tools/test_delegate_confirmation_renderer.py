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
    MAX_DELEGATION_REQUEST_CHARS,
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
async def test_delegate_confirmation_shows_request_in_full_above_generic_bound() -> (
    None
):
    # A request longer than the generic 1200-char field bound but within the
    # delegation budget must still be shown in full so the approver reviews the
    # complete payload.
    request = "x" * (CONFIRMATION_VALUE_MAX_CHARS + 500)
    assert len(request) <= MAX_DELEGATION_REQUEST_CHARS

    prompt = await render_delegate_to_service_confirmation(
        {"target_service_id": "complex_tasks", "user_request": request},
        _no_context(),
    )

    assert request in prompt
    assert "[truncated]" not in prompt
    assert "will be refused" not in prompt


@pytest.mark.asyncio
async def test_delegate_confirmation_refuses_over_limit_request() -> None:
    over_limit = "y" * (MAX_DELEGATION_REQUEST_CHARS + 1)

    prompt = await render_delegate_to_service_confirmation(
        {"target_service_id": "engineer", "user_request": over_limit},
        _no_context(),
    )

    # No partial body is shown (which could be rubber-stamped); the prompt states
    # the hand-off will be refused so the approver isn't misled.
    assert over_limit not in prompt
    assert "will be refused" in prompt
    assert str(len(over_limit)) in prompt
