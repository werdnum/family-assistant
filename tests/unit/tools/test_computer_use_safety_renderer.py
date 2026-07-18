"""Unit tests for the computer-use safety-confirmation renderer.

The confirmation prompt is the sole safeguard before a safety-flagged browser
action runs, so the user must always see which action they are approving, the
model's explanation, and the complete executable payload — with over-length
payloads refused rather than truncated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from family_assistant.tools.computer_use_names import COMPUTER_USE_FUNCTION_NAMES
from family_assistant.tools.confirmation import (
    CONFIRMATION_VALUE_MAX_CHARS,
    TOOL_CONFIRMATION_RENDERERS,
    confirmation_payload_block_reason,
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


def test_block_reason_refuses_over_length_type_text() -> None:
    reason = confirmation_payload_block_reason(
        "type",
        {"text": "x" * (CONFIRMATION_VALUE_MAX_CHARS + 1)},
    )
    assert reason is not None
    assert "smaller pieces" in reason


def test_block_reason_refuses_over_length_navigate_url() -> None:
    reason = confirmation_payload_block_reason(
        "navigate",
        {"url": "https://example.com/?q=" + "x" * CONFIRMATION_VALUE_MAX_CHARS},
    )
    assert reason is not None
    assert "'url'" in reason


def test_block_reason_ignores_safety_decision_metadata() -> None:
    # The safety_decision blob is display-only metadata, not executed payload.
    reason = confirmation_payload_block_reason(
        "click",
        {
            "x": 1,
            "y": 2,
            "safety_decision": {"explanation": "e" * 5000},
        },
    )
    assert reason is None


def test_block_reason_allows_reviewable_arguments() -> None:
    reason = confirmation_payload_block_reason(
        "type",
        {"text": "hello world"},
    )
    assert reason is None
