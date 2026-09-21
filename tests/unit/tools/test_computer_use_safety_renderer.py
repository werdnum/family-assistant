"""Unit tests for the computer-use safety-confirmation renderer.

The confirmation prompt is the sole safeguard before a safety-flagged browser
action runs, so the user must always see which action they are approving, the
model's explanation, and the complete executable payload, rendered in full at
any length.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from family_assistant.tools.computer_use_names import COMPUTER_USE_FUNCTION_NAMES
from family_assistant.tools.confirmation import (
    TOOL_CONFIRMATION_RENDERERS,
    confirmation_arguments_block_reason,
)

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext


def _no_context() -> ToolExecutionContext:
    # The renderer ignores its context argument.
    return cast("ToolExecutionContext", None)


def test_renderer_registered_for_every_computer_use_action() -> None:
    for name in COMPUTER_USE_FUNCTION_NAMES:
        assert name in TOOL_CONFIRMATION_RENDERERS


@pytest.mark.asyncio
async def test_renderer_shows_action_name_explanation_and_arguments() -> None:
    prompt = await TOOL_CONFIRMATION_RENDERERS["click"](
        {
            "x": 450,
            "y": 320,
            "intent": "Click the Confirm Payment button",
            "safety_decision": {
                "decision": "require_confirmation",
                "explanation": "About to confirm a payment",
            },
        },
        _no_context(),
    )

    assert "click" in prompt
    assert "About to confirm a payment" in prompt
    assert "Click the Confirm Payment button" in prompt
    assert "450" in prompt
    assert "320" in prompt


@pytest.mark.asyncio
async def test_renderers_distinguish_coordinate_only_actions() -> None:
    args = {
        "x": 10,
        "y": 20,
        "safety_decision": {"decision": "require_confirmation", "explanation": "e"},
    }
    click_prompt = await TOOL_CONFIRMATION_RENDERERS["click"](args, _no_context())
    right_click_prompt = await TOOL_CONFIRMATION_RENDERERS["right_click"](
        args, _no_context()
    )

    assert "right_click" in right_click_prompt
    assert "right_click" not in click_prompt


@pytest.mark.asyncio
async def test_renderer_handles_missing_safety_decision() -> None:
    prompt = await TOOL_CONFIRMATION_RENDERERS["navigate"](
        {"url": "https://example.com"},
        _no_context(),
    )

    assert "navigate" in prompt
    assert "https://example.com" in prompt


def test_long_computer_use_arguments_are_not_blocked() -> None:
    # Typed text and URLs are rendered in full; whether they can be displayed
    # is decided by the interface delivering the prompt (see
    # docs/design/confirmation-prompt-capacity.md).
    assert confirmation_arguments_block_reason("type", {"text": "x" * 20_000}) is None
    assert (
        confirmation_arguments_block_reason(
            "navigate", {"url": "https://example.com/?q=" + "x" * 20_000}
        )
        is None
    )


def test_block_reason_allows_ordinary_arguments() -> None:
    reason = confirmation_arguments_block_reason(
        "type",
        {"text": "hello world"},
    )
    assert reason is None
