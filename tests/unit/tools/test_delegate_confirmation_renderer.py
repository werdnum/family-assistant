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
    confirmation_payload_block_reason,
    over_length_delegation_block_reason,
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
async def test_delegate_confirmation_shows_resume_reference_and_context_note() -> None:
    # When resuming a prior delegation, the approver must be able to tell they are
    # authorizing reuse of an earlier delegation's history, not a fresh handoff.
    prompt = await render_delegate_to_service_confirmation(
        {
            "target_service_id": "complex_tasks",
            "user_request": "Continue where we left off",
            "resume_delegation_id": "delegation_abc123",
        },
        _no_context(),
    )

    assert "delegation_abc123" in prompt
    assert "context" in prompt.lower()


@pytest.mark.asyncio
async def test_delegate_confirmation_names_a_requested_model_tier() -> None:
    """Spending more is part of what the approver is being asked to authorize."""
    prompt = await render_delegate_to_service_confirmation(
        {
            "target_service_id": "complex_tasks",
            "user_request": "Work out why the brief did not fire",
            "model_tier": "frontier",
        },
        _no_context(),
    )

    assert "frontier" in prompt
    assert "Intelligence" in prompt


@pytest.mark.asyncio
async def test_delegate_confirmation_says_nothing_about_tiers_when_none_was_asked() -> (
    None
):
    prompt = await render_delegate_to_service_confirmation(
        {"target_service_id": "complex_tasks", "user_request": "Start a new task"},
        _no_context(),
    )

    assert "Intelligence" not in prompt


@pytest.mark.asyncio
async def test_delegate_confirmation_omits_resume_note_for_fresh_handoff() -> None:
    prompt = await render_delegate_to_service_confirmation(
        {
            "target_service_id": "complex_tasks",
            "user_request": "Start a new task",
        },
        _no_context(),
    )

    assert "Resuming delegation" not in prompt


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


def test_over_length_block_reason_only_fires_above_the_cap() -> None:
    assert (
        over_length_delegation_block_reason("x" * MAX_DELEGATION_REQUEST_CHARS) is None
    )
    reason = over_length_delegation_block_reason(
        "x" * (MAX_DELEGATION_REQUEST_CHARS + 1)
    )
    assert reason is not None
    assert str(MAX_DELEGATION_REQUEST_CHARS) in reason
    assert "exceeds" in reason


def test_confirmation_payload_block_reason_applies_only_to_scoped_tools() -> None:
    over_limit = "x" * (MAX_DELEGATION_REQUEST_CHARS + 1)

    # An unrelated tool is never size-capped by this hook.
    assert (
        confirmation_payload_block_reason(
            "add_calendar_event", {"user_request": over_limit}
        )
        is None
    )

    # A delegation with an over-limit request is refused.
    assert (
        confirmation_payload_block_reason(
            "delegate_to_service", {"user_request": over_limit}
        )
        is not None
    )
    # A delegation within the cap is allowed through.
    assert (
        confirmation_payload_block_reason(
            "delegate_to_service", {"user_request": "short"}
        )
        is None
    )
