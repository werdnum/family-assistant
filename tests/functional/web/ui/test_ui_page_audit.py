"""Responsive smoke coverage for the web UI's top-level pages."""

import asyncio
from pathlib import Path

import pytest
from playwright.async_api import expect

from tests.functional.web.conftest import WebTestFixture

PAGE_PATHS = [
    "/",
    "/about",
    "/chat",
    "/voice",
    "/notes",
    "/notes/add",
    "/documents/",
    "/documents/upload",
    "/vector-search",
    "/docs/",
    "/automations",
    "/events",
    "/history",
    "/tools",
    "/tasks",
    "/context",
    "/errors",
    "/docs/USER_GUIDE.md",
    "/tool-test-bench",
    "/settings/accounts",
    "/settings/tokens",
]


@pytest.mark.playwright
@pytest.mark.asyncio
@pytest.mark.parametrize("path", PAGE_PATHS)
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [375, 820, 1440])
async def test_pages_fit_viewport(
    web_test_fixture_readonly: WebTestFixture,
    path: str,
    theme: str,
    width: int,
    request: pytest.FixtureRequest,
) -> None:
    """Pages fit phones, tablets and desktops in either selected theme."""
    page = web_test_fixture_readonly.page
    await page.set_viewport_size({"width": width, "height": 900})
    await page.add_init_script(
        f"localStorage.setItem('family-assistant-theme', '{theme}')"
    )
    response = await page.goto(f"{web_test_fixture_readonly.base_url}{path}")
    assert response is not None and response.ok, (
        f"{path} should support direct navigation"
    )
    await expect(page.locator("h1:visible, h2:visible").first).to_be_visible()
    await expect(page.locator('[data-loading-indicator="true"]')).to_have_count(0)
    await expect(page.locator("html")).to_have_class(theme)
    await page.evaluate("document.fonts.ready")
    if request.config.getoption("--take-screenshots", default=False):
        screenshot_dir = Path("scratch/web-ui-audit")
        await asyncio.to_thread(screenshot_dir.mkdir, parents=True, exist_ok=True)
        slug = path.strip("/").replace("/", "-") or "home"
        await page.screenshot(
            path=str(screenshot_dir / f"{slug}-{theme}-{width}.png"), full_page=True
        )
    assert await page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth"
    ), f"{path} overflows the {width}px viewport"


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_mobile_navigation_closes_after_selection(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """A client-side navigation exposes the destination instead of leaving the sheet over it."""
    page = web_test_fixture_readonly.page
    await page.set_viewport_size({"width": 375, "height": 812})
    await page.goto(f"{web_test_fixture_readonly.base_url}/notes")
    await page.get_by_role("button", name="Open navigation menu").click()
    await (
        page.get_by_role("dialog").get_by_role("link", name="Tools", exact=True).click()
    )
    await expect(page).to_have_url(f"{web_test_fixture_readonly.base_url}/tools")
    await expect(page.get_by_role("dialog")).to_be_hidden()
    await expect(
        page.get_by_role("heading", name="Tool Explorer", exact=True).first
    ).to_be_visible()
