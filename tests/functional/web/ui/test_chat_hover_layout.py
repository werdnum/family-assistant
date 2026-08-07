"""Tests that hovering chat messages does not shift surrounding content.

Regression coverage for the action bar layout jump: the per-message action bar
(the Copy button row) autohides on non-last assistant messages and is re-inserted
on hover. If it renders in normal flow rather than floating, its height pushes all
content below it down, so hovering a tool call result made everything jump.
"""

from collections.abc import Callable, Mapping

import pytest
from playwright.async_api import expect

from family_assistant.llm import LLMOutput, ToolCallFunction, ToolCallItem
from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.chat_page import ChatPage
from tests.mocks.mock_llm import RuleBasedMockLLMClient, last_real_message


def _last_message(args: object) -> object | None:
    if not isinstance(args, Mapping):
        return None
    messages = args.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    return last_real_message(messages)


def _last_is_user_containing(text: str) -> Callable[[object], bool]:
    def matcher(args: object) -> bool:
        msg = _last_message(args)
        return (
            msg is not None
            and getattr(msg, "role", None) == "user"
            and text in str(getattr(msg, "content", "")).lower()
        )

    return matcher


def _last_is_tool(args: object) -> bool:
    msg = _last_message(args)
    return msg is not None and getattr(msg, "role", None) == "tool"


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_hovering_tool_result_does_not_shift_content_below(
    web_test_fixture: WebTestFixture,
    mock_llm_client: RuleBasedMockLLMClient,
) -> None:
    """Hovering a non-last assistant message must not push content below it down."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # First turn produces a tool call; second turn is a plain reply. The second
    # turn is what makes the first assistant message non-last, so its action bar
    # autohides and is re-inserted on hover.
    mock_llm_client.rules = [
        (
            _last_is_user_containing("buy milk"),
            LLMOutput(
                content="Sure, saving that note.",
                tool_calls=[
                    ToolCallItem(
                        id="call_note",
                        type="function",
                        function=ToolCallFunction(
                            name="add_or_update_note",
                            arguments='{"title": "Groceries", "content": "Buy milk"}',
                        ),
                    ),
                ],
            ),
        ),
        (_last_is_tool, LLMOutput(content="Saved to your notes.")),
        (_last_is_user_containing("say hi"), LLMOutput(content="Hi there!")),
    ]

    await chat_page.navigate_to_chat()

    await chat_page.send_message("Please remember to buy milk")
    await chat_page.wait_for_message_content("Saved to your notes.", timeout=30000)
    await chat_page.wait_for_tool_call_display(timeout=10000)

    await chat_page.send_message("Now say hi")
    await chat_page.wait_for_message_content("Hi there!", timeout=30000)

    # The first assistant message (with the tool call) is now non-last.
    first_assistant = page.locator(ChatPage.MESSAGE_ASSISTANT).first
    tool_group = first_assistant.locator(ChatPage.TOOL_GROUP)
    await tool_group.wait_for(state="visible", timeout=10000)

    # The second user message sits directly below the first assistant message,
    # so its position is what would move if the action bar grew the layout.
    below = page.locator(ChatPage.MESSAGE_USER).nth(1)
    await below.wait_for(state="visible", timeout=10000)

    action_bar = first_assistant.locator('[data-testid="assistant-action-bar"]')
    # Action bar is autohidden (absent from the DOM) before hovering.
    await expect(action_bar).to_have_count(0)

    before_box = await below.bounding_box()
    assert before_box is not None

    # Hover the tool call result, exactly the interaction from the bug report.
    await tool_group.hover()

    # The action bar appears in floating mode (out of normal flow)...
    await expect(action_bar).to_have_count(1)
    await expect(action_bar).to_have_attribute("data-floating", "true")

    # ...and crucially does not push the content below it down.
    after_box = await below.bounding_box()
    assert after_box is not None
    delta = abs(after_box["y"] - before_box["y"])
    assert delta < 2.0, (
        f"Hovering the tool result shifted content below by {delta}px "
        "(the action bar should float instead of taking layout space)"
    )
