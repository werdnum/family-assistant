"""Unit tests for the spawn_worker / cancel_worker_task confirmation renderers.

Approving spawn_worker launches a code-running agent against the shared
workspace, so the confirmation prompt must show the approver the full task
description (refusing over-length payloads rather than truncating them), the
agent, the context paths that scope what the worker reads, and the timeout.
Cancelling a task must show what is being stopped, not just an opaque id.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from family_assistant.tools.confirmation import (
    CONFIRMATION_VALUE_MAX_CHARS,
    MAX_WORKER_TASK_DESCRIPTION_CHARS,
    TOOL_CONFIRMATION_RENDERERS,
    confirmation_payload_block_reason,
    render_cancel_worker_task_confirmation,
    render_spawn_worker_confirmation,
)

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext


def _no_context() -> ToolExecutionContext:
    # The spawn renderer ignores its context argument.
    return cast("ToolExecutionContext", None)


def _context_with_task(task: dict[str, object] | None) -> ToolExecutionContext:
    context = MagicMock()
    context.db_context.worker_tasks.get_task = AsyncMock(return_value=task)
    return cast("ToolExecutionContext", context)


def test_worker_tools_have_confirmation_renderers() -> None:
    # The engineer profile confirm-gates these tools, so the fallback
    # "Confirm execution of tool: <name>" prompt would hide the payload.
    assert "spawn_worker" in TOOL_CONFIRMATION_RENDERERS
    assert "cancel_worker_task" in TOOL_CONFIRMATION_RENDERERS


@pytest.mark.asyncio
async def test_spawn_worker_confirmation_shows_full_payload() -> None:
    prompt = await render_spawn_worker_confirmation(
        {
            "task_description": "Refactor the parser to stream input",
            "agent": "gemini",
            "context_paths": ["shared/data/input.csv", "shared/scripts/"],
            "timeout_minutes": 45,
        },
        _no_context(),
    )

    assert "Refactor the parser to stream input" in prompt
    assert "gemini" in prompt
    assert "shared/data/input.csv" in prompt
    assert "shared/scripts/" in prompt
    assert "45" in prompt
    assert "sandboxed container" in prompt
    assert "[truncated]" not in prompt


@pytest.mark.asyncio
async def test_spawn_worker_confirmation_shows_description_above_generic_bound() -> (
    None
):
    # The description must be shown in full up to the worker cap, not cut at
    # the generic 1200-char field bound.
    description = "x" * (CONFIRMATION_VALUE_MAX_CHARS + 200)
    prompt = await render_spawn_worker_confirmation(
        {"task_description": description},
        _no_context(),
    )

    assert description in prompt
    assert "[truncated]" not in prompt


@pytest.mark.asyncio
async def test_spawn_worker_confirmation_refuses_over_limit_description() -> None:
    description = "y" * (MAX_WORKER_TASK_DESCRIPTION_CHARS + 1)
    prompt = await render_spawn_worker_confirmation(
        {"task_description": description},
        _no_context(),
    )

    # Never show a partial body the approver might rubber-stamp.
    assert description not in prompt
    assert "will not be launched" in prompt
    assert str(MAX_WORKER_TASK_DESCRIPTION_CHARS) in prompt


@pytest.mark.asyncio
async def test_cancel_worker_task_confirmation_shows_task_details() -> None:
    prompt = await render_cancel_worker_task_confirmation(
        {"task_id": "task-123"},
        _context_with_task({
            "task_id": "task-123",
            "status": "running",
            "task_description": "Build the report generator",
        }),
    )

    assert "task-123" in prompt
    assert "running" in prompt
    assert "Build the report generator" in prompt


@pytest.mark.asyncio
async def test_cancel_worker_task_confirmation_handles_unknown_task() -> None:
    prompt = await render_cancel_worker_task_confirmation(
        {"task_id": "task-gone"},
        _context_with_task(None),
    )

    assert "task-gone" in prompt
    assert "not found" in prompt


def test_spawn_worker_block_reason_fires_only_above_caps() -> None:
    within = {
        "task_description": "z" * MAX_WORKER_TASK_DESCRIPTION_CHARS,
        "context_paths": ["shared/data"],
    }
    assert confirmation_payload_block_reason("spawn_worker", within) is None

    over_description = {
        "task_description": "z" * (MAX_WORKER_TASK_DESCRIPTION_CHARS + 1)
    }
    reason = confirmation_payload_block_reason("spawn_worker", over_description)
    assert reason is not None
    assert "task_description" in reason

    over_paths = {
        "task_description": "ok",
        "context_paths": ["p" * 100 for _ in range(20)],
    }
    reason = confirmation_payload_block_reason("spawn_worker", over_paths)
    assert reason is not None
    assert "context_paths" in reason


def test_cancel_worker_task_is_not_size_capped() -> None:
    # Cancelling only carries an id; the guard must not constrain it.
    assert (
        confirmation_payload_block_reason("cancel_worker_task", {"task_id": "task-123"})
        is None
    )
