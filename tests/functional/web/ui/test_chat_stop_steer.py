"""End-to-end Playwright tests for the web chat Stop and Steer controls.

These drive the real browser against the real backend, gating the (in-process,
same-event-loop) mock LLM on an asyncio.Event so a turn stays "running" long
enough to interact with the Stop button and the steer action (the main composer
doubles as the steer input while a turn runs).
"""

import asyncio
import json

import pytest

from family_assistant.llm import LLMOutput, ToolCallFunction, ToolCallItem
from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.chat_page import ChatPage
from tests.mocks.mock_llm import RuleBasedMockLLMClient


def _gate_first_call(
    mock_llm_client: RuleBasedMockLLMClient,
    monkeypatch: pytest.MonkeyPatch,
    started: asyncio.Event,
    release: asyncio.Event,
) -> None:
    """Park the mock LLM's first generate_response call until ``release`` is set.

    The producer's streaming path delegates to generate_response, so gating it
    holds the turn "running" while Playwright interacts with the live UI. Later
    iterations pass straight through.
    """
    original = mock_llm_client.generate_response
    state = {"gated": False}

    async def gated(*args: object, **kwargs: object) -> LLMOutput:
        if not state["gated"]:
            state["gated"] = True
            started.set()
            await release.wait()
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(mock_llm_client, "generate_response", gated)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_stop_button_cancels_running_turn(
    web_test_fixture: WebTestFixture,
    mock_llm_client: RuleBasedMockLLMClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clicking Stop on a running turn halts it server-side and the bubble
    settles as 'Stopped' (no error)."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)
    await chat_page.navigate_to_chat()

    mock_llm_client.rules = [
        (lambda args: True, LLMOutput(content="reply that should never be seen")),
    ]
    started = asyncio.Event()
    release = asyncio.Event()
    _gate_first_call(mock_llm_client, monkeypatch, started, release)

    await chat_page.send_message("please do something slow")
    # The producer is now parked on the gated LLM call: the turn is running.
    await asyncio.wait_for(started.wait(), timeout=15.0)

    stop_button = page.locator('[data-testid="stop-button"]')
    await stop_button.wait_for(state="visible", timeout=10000)
    await stop_button.click()

    # The turn is cancelled server-side; the assistant bubble settles as stopped.
    await page.wait_for_selector(
        '[data-testid="assistant-message-content"]:has-text("Stopped")',
        timeout=15000,
    )
    # No error placeholder surfaced for a user-initiated stop.
    assert await page.locator("text=/encountered an error/i").count() == 0

    release.set()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_steer_input_injects_midturn_message(
    web_test_fixture: WebTestFixture,
    mock_llm_client: RuleBasedMockLLMClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While a turn runs a tool, typing in the composer and clicking Steer
    injects a mid-turn message that renders as a user bubble and is folded into
    the turn."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)
    await chat_page.navigate_to_chat()

    # Iteration 1 makes a (side-effect-free) tool call so the loop reaches the
    # mid-turn drain; once the steer is in the messages, reply with final text.
    mock_llm_client.rules = [
        (
            lambda args: any(
                msg.role == "user" and "MID-TURN USER UPDATE" in str(msg.content or "")
                for msg in args.get("messages", [])
            ),
            LLMOutput(content="Okay, focusing on tomorrow as you asked."),
        ),
        (
            lambda args: True,
            LLMOutput(
                content="",
                tool_calls=[
                    ToolCallItem(
                        id="call_steer_e2e",
                        type="function",
                        function=ToolCallFunction(
                            name="list_notes", arguments=json.dumps({})
                        ),
                    )
                ],
            ),
        ),
    ]
    started = asyncio.Event()
    release = asyncio.Event()
    _gate_first_call(mock_llm_client, monkeypatch, started, release)

    await chat_page.send_message("plan my week")
    await asyncio.wait_for(started.wait(), timeout=15.0)

    # While a turn runs the main composer doubles as the steer input: typing in
    # it turns the Stop action into Steer, which injects the mid-turn message.
    steer_input = page.locator('[data-testid="chat-input"]')
    await steer_input.wait_for(state="visible", timeout=10000)
    await steer_input.fill("actually, focus on tomorrow")
    await page.locator('[data-testid="steer-button"]').click()

    # Let the turn proceed: it runs the tool, drains the steer, and finishes.
    release.set()

    # The steering message renders as a user bubble...
    await page.wait_for_selector(
        '[data-testid="user-message-content"]:has-text("focus on tomorrow")',
        timeout=15000,
    )
    # ...and the turn's final reply reflects it.
    await page.wait_for_selector(
        '[data-testid="assistant-message-content"]:has-text("focusing on tomorrow")',
        timeout=15000,
    )
