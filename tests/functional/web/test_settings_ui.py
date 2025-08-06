"""Playwright-based functional tests for Settings/Tokens React UI."""

import pytest

from .conftest import WebTestFixture


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_token_management_page_loads(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that token management page loads successfully."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Navigate to token management page
    await page.goto(f"{server_url}/settings/tokens")

    # Wait for page to load
    await page.wait_for_selector("h1:has-text('API Token')", timeout=10000)

    # Check that page has expected structure
    token_container = page.locator("[class*='tokenManagement']")
    await token_container.wait_for(timeout=5000)
    assert await token_container.is_visible()

    # Check for token list or empty state
    token_list = page.locator("[class*='tokenList']")
    empty_state = page.locator("[class*='emptyState']")
    create_button = page.locator("button:has-text('Create')")

    # Should have either tokens or empty state
    has_tokens = await token_list.count() > 0
    has_empty = await empty_state.count() > 0
    has_create = await create_button.count() > 0

    assert has_tokens or has_empty, "Should show token list or empty state"
    assert has_create, "Should have create token button"


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_token_create_form_interaction(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test token creation form interaction."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Navigate to token management page
    await page.goto(f"{server_url}/settings/tokens")
    await page.wait_for_selector("h1:has-text('API Token')", timeout=10000)

    # Look for create button
    create_button = page.locator("button:has-text('Create')")
    if await create_button.count() > 0:
        await create_button.first.click()

        # Wait for form or modal to appear
        await page.wait_for_timeout(1000)

        # Check for form elements
        name_input = page.locator("input[name='name'], input[placeholder*='name' i]")
        submit_button = page.locator(
            "button[type='submit'], button:has-text('Create Token')"
        )
        cancel_button = page.locator("button:has-text('Cancel')")

        # Form elements should be present
        has_name = await name_input.count() > 0
        has_submit = await submit_button.count() > 0
        has_cancel = await cancel_button.count() > 0

        assert has_name or has_submit, "Should show token creation form"

        # Cancel if form is open
        if has_cancel:
            await cancel_button.click()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_token_list_display(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that token list displays properly."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Navigate to token management page
    await page.goto(f"{server_url}/settings/tokens")
    await page.wait_for_selector("h1:has-text('API Token')", timeout=10000)

    # Wait for any API calls to complete
    await page.wait_for_timeout(2000)

    # Check for token items or empty state
    token_items = page.locator("[class*='tokenItem'], [class*='tokenRow']")
    empty_message = page.locator("text=/no.*token/i")

    if await token_items.count() > 0:
        # If tokens exist, check structure
        first_token = token_items.first

        # Should have token details
        token_name = first_token.locator("[class*='tokenName'], [class*='name']")
        revoke_button = first_token.locator("button:has-text('Revoke')")

        has_name = await token_name.count() > 0
        has_revoke = await revoke_button.count() > 0

        assert has_name or has_revoke, "Token items should have details"
    else:
        # Should show empty state
        assert await empty_message.count() > 0, "Should show empty state message"


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_token_responsive_design(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test responsive design of token management page."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Navigate to token management page
    await page.goto(f"{server_url}/settings/tokens")
    await page.wait_for_selector("h1:has-text('API Token')", timeout=10000)

    # Test mobile viewport
    await page.set_viewport_size({"width": 375, "height": 667})
    await page.wait_for_timeout(500)

    # Check that main elements are still visible
    heading = page.locator("h1:has-text('API Token')")
    assert await heading.is_visible()

    # Create button should still be accessible
    create_button = page.locator("button:has-text('Create')")
    if await create_button.count() > 0:
        assert await create_button.is_visible()

    # Test desktop viewport
    await page.set_viewport_size({"width": 1200, "height": 800})
    await page.wait_for_timeout(500)

    # Check elements are still visible
    assert await heading.is_visible()
    if await create_button.count() > 0:
        assert await create_button.is_visible()
