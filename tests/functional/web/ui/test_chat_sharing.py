"""Browser regression coverage for header sharing layout and clipboard recovery."""

from pathlib import Path

import pytest
from playwright.async_api import Request, expect

from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.chat_page import ChatPage
from tests.functional.web.pages.share_conversation_dialog import ShareConversationDialog
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient


@pytest.mark.playwright
@pytest.mark.asyncio
@pytest.mark.parametrize("clipboard_denied", [False, True])
async def test_share_feedback_preserves_header_and_link(
    web_test_fixture: WebTestFixture,
    mock_llm_client: RuleBasedMockLLMClient,
    tmp_path: Path,
    clipboard_denied: bool,
) -> None:
    """Feedback stays out of the header; retry copies the same real read-only URL."""
    page = web_test_fixture.page
    await page.set_viewport_size({"width": 1440, "height": 1000})
    chat = ChatPage(page, web_test_fixture.base_url)
    mock_llm_client.default_response = LLMOutput(
        content="Sharing regression transcript"
    )
    await chat.navigate_to_chat()
    await chat.send_message("A conversation to share")
    await chat.wait_for_message_content("Sharing regression transcript")
    await page.evaluate(
        """(denied) => {
            window.shareCopyDenied = denied;
            window.shareCopies = [];
            Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {
                writeText: async (text) => {
                    if (window.shareCopyDenied) throw new DOMException('Denied', 'NotAllowedError');
                    window.shareCopies.push(text);
                }
            }});
        }""",
        clipboard_denied,
    )
    mutations: list[str] = []

    def record_mutation(request: Request) -> None:
        if request.url.endswith("/share") and request.method in {"POST", "DELETE"}:
            mutations.append(request.method)

    page.on("request", record_mutation)
    sharing = ShareConversationDialog(page)
    await expect(sharing.trigger).to_be_visible()
    before = await sharing.trigger.bounding_box()
    assert before is not None
    await sharing.open()
    await sharing.action("Create and copy link").click()
    await expect(sharing.url).to_be_visible()
    url = await sharing.url.input_value()
    if clipboard_denied:
        await expect(sharing.dialog.get_by_role("alert")).to_contain_text(
            "Could not copy"
        )
        await expect(sharing.url).to_be_focused()
        assert await sharing.url.evaluate(
            "el => el.selectionStart === 0 && el.selectionEnd === el.value.length"
        )
        await page.screenshot(path=str(tmp_path / "share-clipboard-denied.png"))
        await page.evaluate("window.shareCopyDenied = false")
        await sharing.action("Retry copy").click()
    await expect(sharing.dialog.get_by_role("status")).to_have_text("Link copied")
    await expect(sharing.destination).to_have_attribute("href", url)
    assert "/shared/conversations/" in url
    assert await page.evaluate("window.shareCopies") == [url]
    assert mutations == ["POST"]
    await page.screenshot(path=str(tmp_path / "share-copied.png"))
    await sharing.close()
    after = await sharing.trigger.bounding_box()
    assert after == before, f"Share feedback shifted the header: {before} -> {after}"
    await sharing.open()
    await sharing.action("Copy link").click()
    assert await page.evaluate("window.shareCopies") == [url, url]
    assert mutations == ["POST"]

    async with page.expect_popup() as popup_info:
        await sharing.destination.click()
    recipient = await popup_info.value
    await expect(
        recipient.get_by_text("Sharing regression transcript", exact=True)
    ).to_be_visible()
    await expect(recipient.locator(ChatPage.CHAT_INPUT)).to_have_count(0)
    await recipient.close()

    await sharing.action("Stop sharing").click()
    await expect(sharing.dialog.get_by_role("status")).to_contain_text(
        "Sharing stopped"
    )
    await expect(sharing.action("Create and copy link")).to_be_focused()
    await sharing.close()
    assert await sharing.trigger.bounding_box() == before
    assert mutations == ["POST", "DELETE"]
