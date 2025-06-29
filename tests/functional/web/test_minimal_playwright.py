"""Minimal test to verify Playwright setup works."""

import pytest
from playwright.async_api import async_playwright


@pytest.mark.asyncio
async def test_playwright_works() -> None:
    """Test that Playwright can launch a browser."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://example.com")
        title = await page.title()
        assert "Example" in title
        await browser.close()
