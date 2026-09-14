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
    TOOL_CONFIRMATION_RENDERERS,
    confirmation_arguments_block_reason,
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
    context.conversation_id = "conv-1"
    context.db_context.worker_tasks.get_task = AsyncMock(return_value=task)
    return cast("ToolExecutionContext", context)


def test_worker_tools_have_confirmation_renderers() -> None:
    # The engineer profile confirm-gates these tools. The generic fallback would
    # dump their raw arguments; a dedicated renderer says what approving means
    # and enforces the per-field caps this module tests.
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
async def test_spawn_worker_confirmation_shows_a_long_description_in_full() -> None:
    # Nothing is truncated or capped by the renderer: how much of a prompt can
    # be displayed is the delivering interface's call, not the renderer's.
    description = "x" * 20_000
    prompt = await render_spawn_worker_confirmation(
        {"task_description": description},
        _no_context(),
    )

    assert description in prompt
    assert "[truncated]" not in prompt


@pytest.mark.asyncio
async def test_cancel_worker_task_confirmation_shows_task_details() -> None:
    prompt = await render_cancel_worker_task_confirmation(
        {"task_id": "task-123"},
        _context_with_task({
            "task_id": "task-123",
            "conversation_id": "conv-1",
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


@pytest.mark.asyncio
async def test_cancel_worker_task_confirmation_hides_other_conversations_task() -> None:
    # A task belonging to a different conversation is refused on cancel anyway,
    # so the prompt must not leak its details to an approver in this one.
    prompt = await render_cancel_worker_task_confirmation(
        {"task_id": "task-999"},
        _context_with_task({
            "task_id": "task-999",
            "conversation_id": "conv-other",
            "status": "running",
            "task_description": "Some other conversation's private task",
        }),
    )

    assert "not found" in prompt
    assert "Some other conversation's private task" not in prompt


def test_spawn_worker_block_reason_ignores_payload_size() -> None:
    # Size is not a reason to refuse: a huge description and many context paths
    # are rendered in full and left to the interface to deliver.
    assert (
        confirmation_arguments_block_reason(
            "spawn_worker",
            {
                "task_description": "z" * 20_000,
                "context_paths": ["p" * 100 for _ in range(50)],
            },
        )
        is None
    )


def test_spawn_worker_block_reason_rejects_non_list_context_paths() -> None:
    # Script callers bypass JSON-schema validation; a mapping's keys would be
    # iterated as paths by the tool while the prompt showed no paths at all.
    malformed = {
        "task_description": "ok",
        "context_paths": {"shared/secret": True},
    }
    reason = confirmation_arguments_block_reason("spawn_worker", malformed)
    assert reason is not None
    assert "context_paths" in reason
    assert "array" in reason


@pytest.mark.asyncio
async def test_spawn_worker_confirmation_flags_non_list_context_paths() -> None:
    prompt = await render_spawn_worker_confirmation(
        {
            "task_description": "ok",
            "context_paths": {"shared/secret": True},
        },
        _no_context(),
    )

    # The malformed value must be visible as a refusal, never silently omitted.
    assert "Malformed" in prompt
    assert "will not be launched" in prompt


def test_cancel_worker_task_is_not_blocked() -> None:
    # Cancelling only carries an id; the guard must not constrain it.
    assert (
        confirmation_arguments_block_reason(
            "cancel_worker_task", {"task_id": "task-123"}
        )
        is None
    )
