"""Browser regression coverage for sharing, focus, and responsive layout."""

from pathlib import Path

import pytest
import pytest_asyncio
from playwright.async_api import Request, expect

from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.chat_page import ChatPage
from tests.functional.web.pages.share_conversation_dialog import ShareConversationDialog
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient

pytestmark = [pytest.mark.playwright, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def sharing(
    web_test_fixture: WebTestFixture,
    mock_llm_client: RuleBasedMockLLMClient,
) -> ShareConversationDialog:
    """Arrange a persisted conversation and a controllable clipboard."""
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
        """() => {
            window.shareCopyDenied = false;
            window.shareCopies = [];
            Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {
                writeText: async (text) => {
                    if (window.shareCopyDenied) throw new DOMException('Denied', 'NotAllowedError');
                    window.shareCopies.push(text);
                }
            }});
        }"""
    )
    dialog = ShareConversationDialog(page)
    await expect(dialog.trigger).to_be_visible()
    return dialog


@pytest.fixture
def mutations(web_test_fixture: WebTestFixture) -> list[str]:
    """Record real share mutations without replacing the backend."""
    recorded: list[str] = []

    def record(request: Request) -> None:
        if request.url.endswith("/share") and request.method in {"POST", "DELETE"}:
            recorded.append(request.method)

    web_test_fixture.page.on("request", record)
    return recorded


@pytest_asyncio.fixture
async def created_share(sharing: ShareConversationDialog) -> ShareConversationDialog:
    """Arrange a generated URL available in the open dialog."""
    await sharing.open()
    await sharing.action("Create and copy link").click()
    await expect(sharing.dialog.get_by_role("status")).to_have_text("Link copied")
    return sharing


@pytest_asyncio.fixture
async def denied_share(sharing: ShareConversationDialog) -> ShareConversationDialog:
    """Arrange a generated URL whose initial clipboard write was denied."""
    await sharing.page.evaluate("window.shareCopyDenied = true")
    await sharing.open()
    await sharing.action("Create and copy link").click()
    await expect(sharing.dialog.get_by_role("alert")).to_contain_text("Could not copy")
    return sharing


async def test_share_creation_copies_read_only_url(
    sharing: ShareConversationDialog, mutations: list[str], tmp_path: Path
) -> None:
    await sharing.open()
    await sharing.action("Create and copy link").click()

    await expect(sharing.dialog.get_by_role("status")).to_have_text("Link copied")
    url = await sharing.url.input_value()
    await expect(sharing.destination).to_have_attribute("href", url)
    assert "/shared/conversations/" in url
    assert await sharing.page.evaluate("window.shareCopies") == [url]
    assert mutations == ["POST"]
    await sharing.page.screenshot(path=str(tmp_path / "share-copied.png"))


async def test_share_clipboard_denial_selects_url(
    sharing: ShareConversationDialog, tmp_path: Path
) -> None:
    await sharing.page.evaluate("window.shareCopyDenied = true")
    await sharing.open()
    await sharing.action("Create and copy link").click()

    await expect(sharing.dialog.get_by_role("alert")).to_contain_text("Could not copy")
    await expect(sharing.url).to_be_focused()
    assert await sharing.url.evaluate(
        "el => el.selectionStart === 0 && el.selectionEnd === el.value.length"
    )
    await sharing.page.screenshot(path=str(tmp_path / "share-clipboard-denied.png"))


async def test_share_copy_retry_does_not_rotate(
    denied_share: ShareConversationDialog, mutations: list[str]
) -> None:
    sharing = denied_share
    url = await sharing.url.input_value()
    await sharing.page.evaluate("window.shareCopyDenied = false")

    await sharing.action("Retry copy").click()

    await expect(sharing.dialog.get_by_role("status")).to_have_text("Link copied")
    assert await sharing.page.evaluate("window.shareCopies") == [url]
    assert mutations == []


@pytest.mark.parametrize("outcome", ["copied", "denied", "revoked"])
async def test_share_feedback_does_not_shift_header(
    sharing: ShareConversationDialog, outcome: str
) -> None:
    before = await sharing.trigger.bounding_box()
    assert before is not None
    await sharing.page.evaluate(
        "window.shareCopyDenied = " + str(outcome == "denied").lower()
    )

    await sharing.open()
    await sharing.action("Create and copy link").click()
    if outcome == "denied":
        await expect(sharing.dialog.get_by_role("alert")).to_be_visible()
    else:
        await expect(sharing.dialog.get_by_role("status")).to_have_text("Link copied")
    if outcome == "revoked":
        await sharing.action("Stop sharing").click()
        await expect(sharing.dialog.get_by_role("status")).to_contain_text(
            "Sharing stopped"
        )
    await sharing.close()

    assert await sharing.trigger.bounding_box() == before
    await expect(sharing.trigger).to_be_focused()


async def test_share_destination_is_read_only(
    created_share: ShareConversationDialog,
) -> None:
    sharing = created_share

    async with sharing.page.expect_popup() as popup_info:
        await sharing.destination.click()
    recipient = await popup_info.value

    await expect(
        recipient.get_by_text("Sharing regression transcript", exact=True)
    ).to_be_visible()
    await expect(recipient.locator(ChatPage.CHAT_INPUT)).to_have_count(0)
    await recipient.close()


async def test_share_revoke_clears_url_and_restores_focus(
    created_share: ShareConversationDialog, mutations: list[str]
) -> None:
    sharing = created_share

    await sharing.action("Stop sharing").click()

    await expect(sharing.dialog.get_by_role("status")).to_contain_text(
        "Sharing stopped"
    )
    await expect(sharing.url).to_have_count(0)
    await expect(sharing.action("Create and copy link")).to_be_focused()
    assert mutations == ["DELETE"]


@pytest.mark.parametrize("widths", [[600], [600, 1440]])
async def test_share_url_survives_responsive_headers(
    denied_share: ShareConversationDialog, mutations: list[str], widths: list[int]
) -> None:
    sharing = denied_share
    url = await sharing.url.input_value()
    await sharing.close()
    await sharing.page.evaluate("window.shareCopyDenied = false")

    for width in widths:
        await sharing.page.set_viewport_size({"width": width, "height": 1000})
        header_control = (
            ChatPage.BACK_TO_CONVERSATIONS if width < 768 else ChatPage.SIDEBAR_TOGGLE
        )
        await expect(sharing.page.locator(header_control)).to_be_visible()
    await sharing.open()
    await sharing.action("Copy link").click()

    await expect(sharing.url).to_have_value(url)
    await expect(sharing.dialog.get_by_role("status")).to_have_text("Link copied")
    assert await sharing.page.evaluate("window.shareCopies") == [url]
    assert mutations == []
