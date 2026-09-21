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
    confirmation_arguments_block_reason,
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
async def test_delegate_confirmation_shows_a_long_request_in_full() -> None:
    # The renderer applies no size rule of its own: the whole request is shown,
    # and whether an interface can display it is decided at delivery.
    request = "x" * 20_000

    prompt = await render_delegate_to_service_confirmation(
        {"target_service_id": "complex_tasks", "user_request": request},
        _no_context(),
    )

    assert request in prompt
    assert "[truncated]" not in prompt
    assert "will be refused" not in prompt


def test_a_large_delegation_is_not_blocked() -> None:
    # Refusing on size was the old behaviour and is gone: an over-long request
    # is approvable wherever the prompt can be rendered (see
    # docs/design/confirmation-prompt-capacity.md).
    assert (
        confirmation_arguments_block_reason(
            "delegate_to_service", {"user_request": "x" * 20_000}
        )
        is None
    )
    assert (
        confirmation_arguments_block_reason(
            "add_calendar_event", {"user_request": "x" * 20_000}
        )
        is None
    )
