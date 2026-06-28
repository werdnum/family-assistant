"""End-to-end test for the account-global conversation-activity stream.

Proves the web fix for a stale conversation list: a chat started in one tab
appears in a second tab's sidebar live, via `/api/v1/chat/activity/stream`,
without the second tab manually refreshing.
"""

import pytest

from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.chat_page import ChatPage
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_new_chat_in_one_tab_appears_in_another_via_activity_stream(
    web_test_fixture: WebTestFixture,
    mock_llm_client: RuleBasedMockLLMClient,
) -> None:
    """A new conversation started in tab 1 surfaces in tab 2's sidebar live."""
    mock_llm_client.rules = []
    mock_llm_client.default_response = LLMOutput(content="Reply from the assistant.")

    page1 = web_test_fixture.page
    tab1 = ChatPage(page1, web_test_fixture.base_url)

    # Second tab in the SAME browser context, so it shares the session and hits
    # the same backend/account.
    page2 = await page1.context.new_page()
    try:
        tab2 = ChatPage(page2, web_test_fixture.base_url)

        await tab1.navigate_to_chat()

        # Open tab 2 and wait until its activity stream has actually connected
        # (the SSE GET fired) before tab 1 sends. Activity pings are ephemeral —
        # gating on the request makes the test deterministic instead of racing
        # the hook's deferred connect against tab 1's turn.
        async with page2.expect_request(
            lambda request: "/api/v1/chat/activity/stream" in request.url,
            timeout=20000,
        ):
            await tab2.navigate_to_chat()

        # Tab 1 starts a brand-new conversation and sends the first message.
        await tab1.create_new_chat()
        conversation_id = await tab1.get_current_conversation_id()
        assert conversation_id is not None

        await tab1.send_message("Hello from tab one")
        await tab1.wait_for_assistant_response()

        # Tab 2 never refreshes manually; the activity stream must surface the
        # new conversation in its sidebar (the turn pings on both start and end,
        # so tab 2 catches it even if it connected mid-turn).
        await page2.wait_for_selector(
            f'[data-conversation-id="{conversation_id}"]',
            state="visible",
            timeout=20000,
        )
    finally:
        await page2.close()
