"""Loading earlier messages in a long web chat conversation.

The web chat opens a conversation with only its most recent page of history.
"Load earlier messages" widens that window a page at a time, and must keep the
message the reader was looking at in place rather than letting the older rows
arriving above it push it down the screen.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from playwright.async_api import Locator, Page, expect
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm.messages import AssistantMessage, UserMessage
from family_assistant.security.taint import TurnTaintState
from family_assistant.storage.database import Database
from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.chat_page import ChatPage

HISTORY_ROWS = 130
HISTORY_PAGE_SIZE = 50
# Tolerance for the anchored message's on-screen position across a load.
MAX_ANCHOR_SHIFT_PX = 40.0

MESSAGES_SELECTOR = f"{ChatPage.MESSAGE_USER}, {ChatPage.MESSAGE_ASSISTANT}"

# The thread's scroll container is the nearest scrollable ancestor of a message.
FIND_VIEWPORT_JS = """
(el) => {
    let node = el.parentElement;
    while (node) {
        const overflowY = getComputedStyle(node).overflowY;
        if ((overflowY === 'auto' || overflowY === 'scroll')
            && node.scrollHeight > node.clientHeight) {
            return node;
        }
        node = node.parentElement;
    }
    throw new Error('No scrollable thread viewport found');
}
"""

# Rect top after two animation frames, so late layout from the commit has run.
SETTLED_RECT_TOP_JS = """
(el) => new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => {
        resolve(el.getBoundingClientRect().top);
    }));
})
"""


def _history_text(index: int) -> str:
    return f"History message {index:03d}"


async def _seed_history(db_engine: AsyncEngine, conversation_id: str) -> None:
    db = Database(db_engine)
    start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    for index in range(HISTORY_ROWS):
        content = _history_text(index)
        message = (
            UserMessage(content=content)
            if index % 2 == 0
            else AssistantMessage(
                content=content,
                taint_metadata=TurnTaintState.empty().to_metadata(),
            )
        )
        await db.message_history.add_message(
            message,
            interface_type="web",
            conversation_id=conversation_id,
            timestamp=start + timedelta(minutes=index),
            turn_id=f"turn-{index // 2:03d}",
            processing_profile_id="default_assistant",
            user_id="test_user",
        )


def _message_text(page: Page, index: int) -> Locator:
    # Scoped to the thread: the sidebar previews the latest message too.
    return page.locator(MESSAGES_SELECTOR).get_by_text(_history_text(index), exact=True)


async def _rendered_message_count(page: Page) -> int:
    return await page.locator(MESSAGES_SELECTOR).count()


async def _wait_for_more_messages(page: Page, previous_count: int) -> None:
    await page.wait_for_function(
        "([selector, previous]) => document.querySelectorAll(selector).length > previous",
        arg=[MESSAGES_SELECTOR, previous_count],
        timeout=15000,
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_load_earlier_messages_keeps_reader_in_place(
    web_test_fixture: WebTestFixture,
    db_engine: AsyncEngine,
) -> None:
    """Earlier pages load on request without moving the message being read."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)
    conversation_id = f"load-older-{uuid.uuid4().hex[:12]}"
    await _seed_history(db_engine, conversation_id)

    await chat_page.navigate_to_chat(conversation_id)

    newest = _message_text(page, HISTORY_ROWS - 1)
    await expect(newest).to_be_visible(timeout=15000)
    await expect(newest).to_be_in_viewport()
    await expect(_message_text(page, 0)).to_have_count(0)
    load_button = page.get_by_role("button", name="Load earlier messages")
    await expect(load_button).to_have_count(1)
    assert await _rendered_message_count(page) == HISTORY_PAGE_SIZE

    first_rendered = page.locator(MESSAGES_SELECTOR).first
    viewport = await first_rendered.evaluate_handle(FIND_VIEWPORT_JS)
    await viewport.evaluate("(el) => { el.scrollTop = 0; }")
    await page.wait_for_function("(el) => el.scrollTop === 0", arg=viewport)
    await expect(load_button).to_be_in_viewport()

    earliest_index = HISTORY_ROWS - HISTORY_PAGE_SIZE
    await expect(first_rendered).to_contain_text(_history_text(earliest_index))
    anchor = _message_text(page, earliest_index)
    top_before = await anchor.evaluate(SETTLED_RECT_TOP_JS)

    count_before = await _rendered_message_count(page)
    await load_button.click()
    await _wait_for_more_messages(page, count_before)
    await expect(_message_text(page, earliest_index - HISTORY_PAGE_SIZE)).to_have_count(
        1
    )
    top_after = await anchor.evaluate(SETTLED_RECT_TOP_JS)

    assert abs(top_after - top_before) <= MAX_ANCHOR_SHIFT_PX, (
        f"Loading earlier messages moved {_history_text(earliest_index)!r} from "
        f"{top_before:.1f}px to {top_after:.1f}px (the view jumped)"
    )
