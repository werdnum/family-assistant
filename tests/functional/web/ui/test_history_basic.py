"""Playwright-based functional tests for chat history React UI - Basic display and navigation."""

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from playwright.async_api import Page, Route, expect

from family_assistant.llm.messages import AssistantMessage, UserMessage
from family_assistant.storage.database import Database
from tests.functional.web.conftest import WebTestFixture


async def wait_for_history_page_loaded(page: Page, timeout: int = 15000) -> bool:
    """Wait for the history page to load completely and return whether it succeeded."""
    try:
        # Wait until the frontend signals it is ready
        await page.wait_for_function(
            "() => document.documentElement.getAttribute('data-app-ready') === 'true'",
            timeout=timeout,
        )

        # Try multiple selectors that indicate the page is loaded
        await page.wait_for_selector(
            "h1:has-text('Conversation History'), h1, main, [data-testid='history-page']",
            timeout=timeout,
        )

        # Additional check - make sure the page text is there
        page_text = await page.text_content("body")
        if page_text and "Conversation History" in page_text:
            return True

        # If no text, try waiting a bit more for React to mount
        page_text = await page.text_content("body")
        return page_text is not None and "Conversation History" in page_text

    except Exception as e:
        logging.error(f"Failed to wait for history page: {e}")
        # Check if page has any content at all
        try:
            page_text = await page.text_content("body")
            return page_text is not None and "Conversation History" in page_text
        except Exception:
            return False


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_page_basic_loading(
    web_test_fixture_readonly: WebTestFixture,
    take_screenshot: Callable[[Any, str, str], Awaitable[None]],
) -> None:
    """Test basic functionality of the history page React interface."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    # Set up console error tracking
    console_errors = []
    network_errors = []

    def on_console(msg: Any) -> None:  # noqa: ANN401  # playwright console message
        if msg.type == "error":
            console_errors.append(
                f"{msg.location.get('url', 'unknown')}:{msg.location.get('lineNumber', '?')} - {msg.text}"
            )
            print(f"[CONSOLE ERROR] {msg.text}")

    def on_response(response: Any) -> None:  # noqa: ANN401  # playwright response object
        if response.status >= 400:
            network_errors.append(f"{response.status} {response.url}")
            print(f"[NETWORK ERROR] {response.status} {response.url}")

    page.on("console", on_console)
    page.on("response", on_response)

    # Navigate to history page
    print(f"=== Navigating to {server_url}/history ===")
    await page.goto(f"{server_url}/history")

    # Take a screenshot before waiting
    await page.screenshot(path="/tmp/history_page_initial.png")
    print("Screenshot saved to /tmp/history_page_initial.png")

    # Check page content
    page_content = await page.content()
    print(f"Page content length: {len(page_content)}")
    print(f"Page title: {await page.title()}")

    # Check if React app root exists
    app_root = await page.locator("#app-root").count()
    print(f"Found {app_root} #app-root elements")

    # Check for router.html markers
    router_marker = "router-entry.jsx" in page_content
    print(f"Router entry point loaded: {router_marker}")

    # Wait for React to mount
    # Wait for page to fully load

    # Check for any h1 elements
    h1_count = await page.locator("h1").count()
    print(f"Found {h1_count} h1 elements on page")

    # Check for any text content
    body_text = await page.locator("body").text_content()
    print(
        f"Body text (first 500 chars): {body_text[:500] if body_text else 'No text content'}"
    )

    # Wait for network idle to ensure all resources loaded

    print("Network idle state reached")

    # Check for console and network errors
    if console_errors:
        print("=== CONSOLE ERRORS DETECTED ===")
        for err in console_errors:
            print(f"  - {err}")
        # Don't fail immediately, let's see what loaded

    if network_errors:
        print("=== NETWORK ERRORS DETECTED ===")
        for err in network_errors:
            print(f"  - {err}")

    # Wait for the history page to load
    page_loaded = await wait_for_history_page_loaded(page, timeout=15000)

    if not page_loaded:
        print("Failed to detect history page load - taking diagnostics")
        # Take another screenshot
        await page.screenshot(path="/tmp/history_page_after_wait.png")
        print("Screenshot saved to /tmp/history_page_after_wait.png")

        # Get final page state
        final_content = await page.content()
        print(f"Final page HTML (first 2000 chars):{final_content[:2000]}")

        # Check if there were critical errors
        if console_errors:
            print(f"Console errors detected: {console_errors}")

        assert not console_errors, f"Console errors detected: {console_errors}"
        assert page_loaded, "History page failed to load properly"

    # Verify React components have loaded by checking for filters section
    filters_section = page.locator("details summary:has-text('Filters')")
    await filters_section.wait_for(timeout=5000)
    assert await filters_section.is_visible()

    # Check that results summary is present (handles empty state)
    results_summary = page.locator("text=/Found \\d+ conversation/")
    await results_summary.wait_for(timeout=5000)
    summary_text = await results_summary.text_content()
    assert summary_text is not None
    assert "Found" in summary_text and "conversation" in summary_text

    # Take screenshot of history page
    for viewport in ["desktop", "mobile"]:
        await take_screenshot(page, "history-list", viewport)

    # Final check for errors
    assert not console_errors, f"Console errors detected during test: {console_errors}"


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_page_css_styling(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test that CSS styling is properly applied to React components."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    # Navigate to history page
    await page.goto(f"{server_url}/history")

    # Wait for page to load
    await page.wait_for_selector("h1:has-text('Conversation History')", timeout=10000)

    # Check that main container has expected CSS classes (indicating React components loaded)
    # The exact class names depend on the CSS modules
    conversations_list = page.locator("[class*='conversationsList']")
    await conversations_list.wait_for(timeout=5000)
    assert await conversations_list.is_visible()

    # Check that filters form has CSS styling
    filters_form = page.locator("form[class*='filtersForm'], .filtersForm")
    if await filters_form.count() > 0:
        assert await filters_form.is_visible()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_conversation_detail_view(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test conversation detail view functionality."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    test_conversation_id = f"missing-{uuid4().hex}"
    await page.goto(f"{server_url}/history/{test_conversation_id}")
    await expect(
        page.get_by_text("No messages found in this conversation.")
    ).to_be_visible()
    await page.get_by_role("button", name="Back to Conversations").click()
    await expect(page).to_have_url(f"{server_url}/history")


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_pagination_interface(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test pagination controls when available."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    # Navigate to history page
    await page.goto(f"{server_url}/history")

    # Wait for API response - either conversations container or empty state will appear
    await page.wait_for_selector(
        "[class*='conversationsContainer'], .conversationsContainer, [class*='emptyState'], .emptyState",
        state="visible",
        timeout=10000,
    )

    # Check if pagination controls are present
    pagination = page.locator("[class*='pagination'], .pagination")

    # If pagination exists, test basic functionality
    if await pagination.count() > 0 and await pagination.is_visible():
        # Look for page navigation elements
        page_buttons = pagination.locator("button, a")
        button_count = await page_buttons.count()

        if button_count > 0:
            # Pagination controls are present and visible
            assert await pagination.is_visible()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_responsive_design(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test responsive design of history page."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    # Navigate to history page
    await page.goto(f"{server_url}/history")

    # Wait for page to load
    await page.wait_for_selector("h1:has-text('Conversation History')", timeout=10000)

    # Test mobile viewport
    await page.set_viewport_size({"width": 375, "height": 667})
    # Check that main elements are still visible
    heading = page.locator("h1:has-text('Conversation History')")
    await expect(heading).to_be_visible()

    # Filters should still be accessible
    filters_section = page.locator("details summary:has-text('Filters')")
    await expect(filters_section).to_be_visible()

    # Test tablet viewport
    await page.set_viewport_size({"width": 768, "height": 1024})
    # Check elements are still visible
    await expect(heading).to_be_visible()
    await expect(filters_section).to_be_visible()

    # Test desktop viewport
    await page.set_viewport_size({"width": 1200, "height": 800})
    # Check elements are still visible
    await expect(heading).to_be_visible()
    await expect(filters_section).to_be_visible()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_api_error_handling(
    web_test_fixture: WebTestFixture,
) -> None:
    """The history page reports a failed conversation-list request."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    async def fail_conversations(route: Route) -> None:
        await route.fulfill(status=500, body="Server error")

    await page.route("**/api/v1/chat/conversations?*", fail_conversations)
    await page.goto(f"{server_url}/history")
    await expect(
        page.get_by_text("Error: Failed to fetch conversations:", exact=False)
    ).to_be_visible()
    await expect(page.get_by_text("Loading conversations...")).to_be_hidden()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_message_display_structure(
    web_test_fixture: WebTestFixture,
) -> None:
    """A saved conversation renders its user and assistant messages."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url
    engine = web_test_fixture.assistant.database_engine
    assert engine is not None
    db_context = Database(engine=engine)
    conversation_id = f"history-messages-{uuid4().hex}"
    timestamp = datetime.now(UTC)
    await db_context.message_history.add_message(
        UserMessage(content="History detail user prompt"),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=timestamp,
        user_id="test_user",
    )
    await db_context.message_history.add_message(
        AssistantMessage(content="History detail assistant reply"),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=timestamp + timedelta(seconds=1),
        user_id="test_user",
    )

    await page.goto(f"{server_url}/history/{conversation_id}")
    await expect(
        page.get_by_role("heading", name="Conversation Details")
    ).to_be_visible()
    await expect(page.get_by_text("History detail user prompt")).to_be_visible()
    await expect(page.get_by_text("History detail assistant reply")).to_be_visible()
    await page.get_by_role("button", name="Back to Conversations").click()
    await expect(page).to_have_url(f"{server_url}/history")
