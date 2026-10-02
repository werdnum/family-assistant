"""Playwright-based functional tests for Documentation React UI."""

import re
from collections.abc import Awaitable, Callable

import pytest
from playwright.async_api import Page, expect

from tests.functional.web.conftest import WebTestFixture


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_documentation_list_page_loads(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """The documentation list includes the user guide after loading."""
    page = web_test_fixture_readonly.page

    await page.goto(f"{web_test_fixture_readonly.base_url}/docs")

    await expect(page.get_by_role("heading", name="Documentation")).to_be_visible()
    await expect(
        page.get_by_role("link", name=re.compile(r"USER_GUIDE\.md"))
    ).to_be_visible(timeout=10000)
    await expect(page.get_by_text("Error loading documentation")).to_have_count(0)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_documentation_view_page_loads(
    web_test_fixture_readonly: WebTestFixture,
    take_screenshot: Callable[[Page, str, str], Awaitable[None]],
) -> None:
    """The guide route renders the actual guide content."""
    page = web_test_fixture_readonly.page

    await page.goto(f"{web_test_fixture_readonly.base_url}/docs/USER_GUIDE.md")

    await expect(
        page.get_by_role("heading", name="Family Assistant User Guide")
    ).to_be_visible(timeout=10000)
    await expect(page.get_by_text("Error loading documentation")).to_have_count(0)
    for viewport in ["desktop", "mobile"]:
        await take_screenshot(page, "documentation-reader", viewport)
        assert await page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        ), "Documentation reader overflows its viewport"


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_documentation_navigation(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """The guide link opens the guide and its back button returns to the list."""
    page = web_test_fixture_readonly.page

    await page.goto(f"{web_test_fixture_readonly.base_url}/docs")
    await page.get_by_role("link", name=re.compile(r"USER_GUIDE\.md")).click(
        timeout=10000
    )

    await expect(page).to_have_url(re.compile(r"/docs/USER_GUIDE\.md$"))
    await expect(
        page.get_by_role("heading", name="Family Assistant User Guide")
    ).to_be_visible(timeout=10000)

    await page.get_by_role("button", name="Documentation").click()

    await expect(page).to_have_url(re.compile(r"/docs/?$"))
    await expect(
        page.get_by_role("link", name=re.compile(r"USER_GUIDE\.md"))
    ).to_be_visible(timeout=10000)
