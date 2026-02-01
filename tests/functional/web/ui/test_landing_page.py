"""End-to-end tests for the landing page and navigation."""

import pytest
from playwright.async_api import expect

from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.chat_page import ChatPage
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_landing_page_rendering(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test that the landing page renders correctly."""
    page = web_test_fixture_readonly.page
    base_url = web_test_fixture_readonly.base_url

    # Navigate to root
    await page.goto(base_url)
    await page.wait_for_selector('[data-app-ready="true"]', timeout=10000)

    # Check title
    await expect(page.locator("h1")).to_contain_text("Family Assistant")

    # Check for feature cards
    await expect(page.locator("main").get_by_text("Chat", exact=True)).to_be_visible()
    await expect(page.locator("main").get_by_text("Voice Mode")).to_be_visible()
    await expect(page.locator("main").get_by_text("Notes", exact=True)).to_be_visible()
    await expect(
        page.locator("main").get_by_text("Documents", exact=True)
    ).to_be_visible()
    await expect(
        page.locator("main").get_by_text("Automations", exact=True)
    ).to_be_visible()
    await expect(
        page.locator("main").get_by_text("History", exact=True)
    ).to_be_visible()

    # Check for chat input
    await expect(
        page.locator("input[placeholder='How can I help you today?']")
    ).to_be_visible()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_landing_page_query_param(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test that the ?q= query parameter triggers a new chat."""
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url
    chat_page = ChatPage(page, base_url)

    # Configure mock LLM response
    mock_llm_client.rules = [
        (
            lambda args: "test query" in str(args.get("messages", [])),
            LLMOutput(content="I received your test query!"),
        )
    ]

    # Navigate to /chat with q parameter
    query = "test query"
    await page.goto(f"{base_url}/chat?q={query}")

    # Wait for the app to be ready
    await page.wait_for_selector('[data-app-ready="true"]', timeout=10000)

    # Verify we are on the chat page
    assert "/chat" in page.url

    # Wait for the assistant response
    await chat_page.wait_for_messages_with_content(
        {
            "user": query,
            "assistant": "received your test query",
        },
        timeout=20000,
    )

    # Verify conversation ID was created
    conv_id = await chat_page.get_current_conversation_id()
    assert conv_id is not None
    assert conv_id.startswith("web_conv_")


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_landing_page_search_navigation(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test that submitting the form on the landing page navigates to chat."""
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url
    chat_page = ChatPage(page, base_url)

    # Configure mock LLM response
    mock_llm_client.rules = [
        (
            lambda args: "hello from landing" in str(args.get("messages", [])),
            LLMOutput(content="Hello! I saw you came from the landing page."),
        )
    ]

    # Navigate to landing page
    await page.goto(base_url)
    await page.wait_for_selector('[data-app-ready="true"]', timeout=10000)

    # Fill in the chat input
    prompt = "hello from landing"
    await page.fill("input[placeholder='How can I help you today?']", prompt)
    await page.click("button[type='submit']")

    # Verify navigation
    await page.wait_for_url("**/chat?q=*", timeout=10000)
    assert "/chat" in page.url

    # Wait for ChatApp to process the query
    await chat_page.wait_for_messages_with_content(
        {
            "user": prompt,
            "assistant": "came from the landing page",
        },
        timeout=20000,
    )
