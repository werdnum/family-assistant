"""Rendered message Copy controls, using the real chat and assistant-ui runtime."""

import pytest
from playwright.async_api import Locator, expect

from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.chat_page import ChatPage
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient

RESPONSE = (
    "Here is **bold text**. Copy `sample code` and a [link](https://example.com)."
)


async def _open_reply(fixture: WebTestFixture, llm: RuleBasedMockLLMClient) -> ChatPage:
    page = fixture.page
    await page.set_viewport_size({"width": 1440, "height": 900})
    llm.default_response = LLMOutput(content=RESPONSE)
    chat = ChatPage(page, fixture.base_url)
    await chat.navigate_to_chat()
    await chat.send_message("Give me a formatted reply")
    await chat.wait_for_message_content("Here is bold text.")
    await expect(page.locator('[data-testid="assistant-action-bar"]')).to_have_count(1)
    return chat


async def _assert_icon_geometry(button: Locator) -> None:
    button_box = await button.bounding_box()
    icon_box = await button.locator("svg").bounding_box()
    assert button_box is not None and icon_box is not None
    assert button_box["width"] == pytest.approx(28)
    assert button_box["height"] == pytest.approx(28)
    assert icon_box["width"] == pytest.approx(12)
    assert icon_box["height"] == pytest.approx(12)
    assert icon_box["x"] + 6 == pytest.approx(button_box["x"] + 14)
    assert icon_box["y"] + 6 == pytest.approx(button_box["y"] + 14)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_copy_hover_has_accessible_name_and_full_size_icon(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    await _open_reply(web_test_fixture, mock_llm_client)
    page = web_test_fixture.page
    bar = page.get_by_test_id("assistant-action-bar")
    await page.mouse.move(0, 0)
    await expect(bar).to_have_css("opacity", "0")
    await page.get_by_test_id("assistant-message-content").hover()
    button = page.get_by_role("button", name="Copy response", exact=True)
    await expect(bar).to_have_css("opacity", "1")
    await expect(bar).to_have_css("pointer-events", "auto")
    await _assert_icon_geometry(button)
    await button.hover()
    await expect(page.get_by_role("tooltip")).to_have_text("Copy")


@pytest.mark.playwright
@pytest.mark.asyncio
@pytest.mark.parametrize("older_reply", [False, True])
async def test_copy_keyboard_focus_reveals_latest_and_older_controls(
    web_test_fixture: WebTestFixture,
    mock_llm_client: RuleBasedMockLLMClient,
    older_reply: bool,
) -> None:
    chat = await _open_reply(web_test_fixture, mock_llm_client)
    page = web_test_fixture.page
    if older_reply:
        mock_llm_client.default_response = LLMOutput(content="A second reply.")
        await chat.send_message("Reply again")
        await chat.wait_for_message_content("A second reply.")
        await expect(page.get_by_test_id("assistant-action-bar")).to_have_count(2)
    button = page.get_by_role("button", name="Copy response", exact=True).first
    bar = page.get_by_test_id("assistant-action-bar").first
    await page.mouse.move(0, 0)
    await expect(bar).to_have_css("opacity", "0")
    await chat.tab_to_message_copy()
    await expect(button).to_be_focused()
    await expect(bar).to_have_css("opacity", "1")
    await expect(bar).to_have_css("pointer-events", "auto")
    await expect(page.get_by_role("tooltip")).to_have_text("Copy")
    await _assert_icon_geometry(button)
    await page.keyboard.press("Tab")
    await expect(bar).to_have_css("opacity", "0")


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_copy_writes_response_text_and_renders_full_size_checkmark(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    chat = await _open_reply(web_test_fixture, mock_llm_client)
    page = web_test_fixture.page
    await page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    button = page.get_by_role("button", name="Copy response", exact=True)
    await chat.tab_to_message_copy()
    await page.keyboard.press("Enter")
    await expect(
        page.get_by_role("status").filter(has_text="Response copied")
    ).to_be_attached()
    assert await page.evaluate("navigator.clipboard.readText()") == RESPONSE
    await expect(button.locator("svg.lucide-check")).to_have_count(1)
    await _assert_icon_geometry(button)
    await expect(page.get_by_role("tooltip")).to_have_text("Copied")
    await expect(button.locator("svg.lucide-copy")).to_have_count(1)


@pytest.mark.playwright
@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", [False, True])
async def test_copy_failure_is_visible_after_blur_without_success(
    web_test_fixture: WebTestFixture,
    mock_llm_client: RuleBasedMockLLMClient,
    unavailable: bool,
) -> None:
    chat = await _open_reply(web_test_fixture, mock_llm_client)
    page = web_test_fixture.page
    await page.evaluate(
        """unavailable => Object.defineProperty(navigator, 'clipboard', {
            configurable: true,
            value: unavailable ? undefined : {
                writeText: () => Promise.reject(new DOMException('Denied', 'NotAllowedError'))
            }
        })""",
        unavailable,
    )
    button = page.get_by_role("button", name="Copy response", exact=True)
    await chat.tab_to_message_copy()
    await page.keyboard.press("Enter")
    await page.keyboard.press("Tab")
    await expect(page.get_by_role("alert")).to_have_text(
        "Couldn't copy response. Select the text and copy it manually."
    )
    await expect(page.get_by_role("alert")).to_be_visible()
    await expect(button.locator("svg.lucide-check")).to_have_count(0)
    await expect(
        page.get_by_role("status").filter(has_text="Response copied")
    ).to_have_count(0)
