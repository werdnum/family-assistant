"""Unit tests for the one renderer every confirmation path goes through."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from family_assistant.tools.confirmation import render_tool_confirmation

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext


def _context(review_reason: str | None = None) -> ToolExecutionContext:
    return cast(
        "ToolExecutionContext",
        SimpleNamespace(tool_call_review_confirmation_reason=review_reason),
    )


@pytest.mark.asyncio
async def test_tool_without_renderer_shows_its_arguments() -> None:
    prompt = await render_tool_confirmation(
        "ha_call_service",
        {"service": "turn_off", "entity_id": "light.kitchen"},
        _context(),
    )

    assert "ha_call_service" in prompt
    assert "light.kitchen" in prompt


@pytest.mark.asyncio
async def test_review_reason_is_appended() -> None:
    prompt = await render_tool_confirmation(
        "ha_call_service", {"service": "turn_off"}, _context("Came from an email.")
    )

    assert prompt.endswith("Automatic review reason:\n> Came from an email.")
