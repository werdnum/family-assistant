"""Playwright-based functional tests for Settings/Tokens React UI."""

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from playwright.async_api import expect

from tests.functional.web.conftest import WebTestFixture


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_token_management_page_loads(
    web_test_fixture_readonly: WebTestFixture,
    take_screenshot: Callable[[Any, str, str], Awaitable[None]],
) -> None:
    """Test that token management page loads successfully."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    await page.goto(f"{server_url}/settings/tokens")
    await expect(
        page.get_by_role("heading", name="API Token Management")
    ).to_be_visible()
    await expect(
        page.get_by_role("heading", name="Your Tokens", exact=False)
    ).to_be_visible()
    await expect(page.get_by_role("button", name="Create New Token")).to_be_visible()

    # Take screenshot of settings/tokens page
    for viewport in ["desktop", "mobile"]:
        await take_screenshot(page, "settings-tokens", viewport)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_token_create_form_interaction(
    web_test_fixture: WebTestFixture,
) -> None:
    """Create a token and show its secret and list entry."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    await page.goto(f"{server_url}/settings/tokens")
    await expect(page.get_by_role("heading", name="Your Tokens (0)")).to_be_visible()
    await page.get_by_role("button", name="Create New Token").click()
    await page.get_by_role("textbox", name="Token Name").fill("Browser test token")
    await page.get_by_role("button", name="Create Token", exact=True).click()

    success = (
        page
        .locator("div")
        .filter(has=page.get_by_text("Token Created Successfully!"))
        .filter(has=page.get_by_role("button", name="Copy"))
        .last
    )
    await expect(success).to_be_visible()
    full_token = await success.locator("code").text_content()
    assert full_token is not None and len(full_token) > 8
    await expect(page.get_by_role("heading", name="Your Tokens (1)")).to_be_visible()
    token_entry = (
        page
        .locator("div")
        .filter(has=page.get_by_role("heading", name="Browser test token"))
        .filter(has=page.get_by_role("button", name="Revoke"))
        .last
    )
    await expect(token_entry).to_be_visible()
    await expect(token_entry.locator("code")).to_contain_text(full_token[:8])


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_token_list_display(
    web_test_fixture: WebTestFixture,
) -> None:
    """Show the empty state when the user has no tokens."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    await page.goto(f"{server_url}/settings/tokens")
    await expect(page.get_by_role("heading", name="Your Tokens (0)")).to_be_visible()
    await expect(page.get_by_text("No API tokens found", exact=False)).to_be_visible()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_token_responsive_design(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test responsive design of token management page."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    await page.set_viewport_size({"width": 375, "height": 667})
    await page.goto(f"{server_url}/settings/tokens")
    create_button = page.get_by_role("button", name="Create New Token")
    await expect(create_button).to_be_visible()
    await create_button.click()
    await expect(page.get_by_role("textbox", name="Token Name")).to_be_visible()
    await expect(
        page.get_by_role("button", name="Create Token", exact=True)
    ).to_be_visible()
