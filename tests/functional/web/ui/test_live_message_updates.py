"""Playwright tests for live message updates across browser contexts."""

import re

import pytest
from playwright.async_api import Browser, BrowserContext, Page

from tests.functional.web.conftest import WebTestFixture


async def navigate_to_chat(page: Page, base_url: str) -> None:
    """Navigate a page to the chat interface and wait for it to load."""
    await page.goto(f"{base_url}/chat")


async def send_message(page: Page, message: str) -> None:
    """Type and send a message in the chat interface."""
    message_input = await page.wait_for_selector(
        'textarea[placeholder*="message" i], textarea[placeholder*="Type" i]',
        timeout=5000,
    )
    assert message_input is not None  # Type narrowing for type checker
    await message_input.fill(message)
    # Press Enter to send (same approach as ChatPage)
    await message_input.press("Enter")


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_message_appears_in_second_context(
    browser: Browser, web_test_fixture: WebTestFixture
) -> None:
    """Test that sending a message in one context shows up in another context."""
    base_url = web_test_fixture.base_url
    page1 = web_test_fixture.page

    # Create a second browser context (simulates a second user or tab)
    context2: BrowserContext = await browser.new_context()
    page2: Page = await context2.new_page()

    try:
        # Navigate page1 to chat first
        await navigate_to_chat(page1, base_url)

        # Wait for conversation to be created and URL to be updated
        # ChatApp creates a new conversation on mount if none exists
        await page1.wait_for_function(
            "() => window.location.href.includes('conversation_id=')",
            timeout=5000,
        )

        # Get the conversation ID from page1's URL
        page1_url = page1.url
        match = re.search(r"conversation_id=([^&]+)", page1_url)
        if not match:
            raise RuntimeError(f"Could not find conversation_id in URL: {page1_url}")
        conversation_id = match.group(1)

        # Navigate page2 to the SAME conversation and wait for SSE connection
        # Set up listener for SSE connection BEFORE navigation to ensure we don't miss it
        async with page2.expect_response(
            lambda r: "/api/v1/chat/events" in r.url and r.ok, timeout=30000
        ):
            await page2.goto(f"{base_url}/chat?conversation_id={conversation_id}")

            # SSE connection will be established within the context manager

        # Now send the test message (SSE is connected, so it will be delivered)
        test_message = "Test message for live updates"
        await send_message(page1, test_message)

        # Wait for message to appear in page1
        await page1.wait_for_selector(
            f'text="{test_message}"', state="visible", timeout=10000
        )

        # The key assertion: message should appear in page2 WITHOUT refresh
        await page2.wait_for_selector(
            f'text="{test_message}"', state="visible", timeout=15000
        )

    finally:
        # Clean up second context
        await page2.close()
        await context2.close()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_bidirectional_live_updates(
    browser: Browser, web_test_fixture: WebTestFixture
) -> None:
    """Test that messages sent from either context appear in both contexts."""
    base_url = web_test_fixture.base_url
    page1 = web_test_fixture.page

    # Create a second browser context
    context2: BrowserContext = await browser.new_context()
    page2: Page = await context2.new_page()

    try:
        # Navigate page1 to chat first
        await navigate_to_chat(page1, base_url)

        # Wait for conversation to be created and URL to be updated
        # ChatApp creates a new conversation on mount if none exists
        await page1.wait_for_function(
            "() => window.location.href.includes('conversation_id=')",
            timeout=5000,
        )

        # Get the conversation ID from page1's URL
        page1_url = page1.url
        match = re.search(r"conversation_id=([^&]+)", page1_url)
        if not match:
            raise RuntimeError(f"Could not find conversation_id in URL: {page1_url}")
        conversation_id = match.group(1)

        # Navigate page2 to the SAME conversation and wait for SSE connection
        # Set up listener for SSE connection BEFORE navigation to ensure we don't miss it
        async with page2.expect_response(
            lambda r: "/api/v1/chat/events" in r.url and r.ok, timeout=30000
        ):
            await page2.goto(f"{base_url}/chat?conversation_id={conversation_id}")

            # SSE connection will be established within the context manager

        # Now send test message from page1 (SSE is connected, so it will be delivered)
        message_from_page1 = "Message from first context"
        await send_message(page1, message_from_page1)

        # Wait for it to appear in both contexts
        await page1.wait_for_selector(
            f'text="{message_from_page1}"', state="visible", timeout=10000
        )
        await page2.wait_for_selector(
            f'text="{message_from_page1}"', state="visible", timeout=15000
        )

        # Send message from page2
        message_from_page2 = "Message from second context"
        await send_message(page2, message_from_page2)

        # Wait for it to appear in both contexts
        await page2.wait_for_selector(
            f'text="{message_from_page2}"', state="visible", timeout=10000
        )
        await page1.wait_for_selector(
            f'text="{message_from_page2}"', state="visible", timeout=15000
        )

    finally:
        # Clean up second context
        await page2.close()
        await context2.close()
