"""Loading earlier messages in a long web chat conversation.

The web chat opens a conversation with only its most recent page of history.
"Load earlier messages" widens that window a page at a time, and must keep the
message the reader was looking at in place rather than letting the older rows
arriving above it push it down the screen.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from playwright.async_api import expect
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


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_load_earlier_messages_keeps_reader_in_place(
    web_test_fixture: WebTestFixture,
    db_engine: AsyncEngine,
) -> None:
    """Earlier pages load on request without moving the message being read."""
    chat_page = ChatPage(web_test_fixture.page, web_test_fixture.base_url)
    conversation_id = f"load-older-{uuid.uuid4().hex[:12]}"
    await _seed_history(db_engine, conversation_id)

    await chat_page.navigate_to_chat(conversation_id)
    await expect(
        chat_page.message_with_text(_history_text(HISTORY_ROWS - 1))
    ).to_be_visible(timeout=15000)
    await expect(chat_page.message_with_text(_history_text(0))).to_have_count(0)
    assert await chat_page.rendered_message_count() == HISTORY_PAGE_SIZE

    await chat_page.scroll_thread_to_top()
    await expect(chat_page.load_earlier_messages_button()).to_be_in_viewport()
    anchor_text = _history_text(HISTORY_ROWS - HISTORY_PAGE_SIZE)
    anchor = chat_page.message_with_text(anchor_text)
    await expect(anchor).to_be_in_viewport()
    top_before = await ChatPage.settled_top(anchor)

    await chat_page.load_earlier_messages()
    await expect(
        chat_page.message_with_text(_history_text(HISTORY_ROWS - 2 * HISTORY_PAGE_SIZE))
    ).to_have_count(1)
    top_after = await ChatPage.settled_top(anchor)

    assert abs(top_after - top_before) <= MAX_ANCHOR_SHIFT_PX, (
        f"Loading earlier messages moved {anchor_text!r} from "
        f"{top_before:.1f}px to {top_after:.1f}px (the view jumped)"
    )
