"""Stealth browser utilities for reduced automation detection.

This module provides centralized configuration for Playwright browser sessions
with anti-detection measures. It uses rebrowser-playwright which patches
Playwright to avoid common automation detection mechanisms.

Key features:
- Realistic Chrome user agent strings
- Browser args to disable automation detection flags
- Centralized launch configuration for consistency
"""

from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Playwright


class ViewportSize(TypedDict):
    """Viewport dimensions for browser context."""

    width: int
    height: int


logger = logging.getLogger(__name__)

# Modern Chrome user agents for different platforms
# Updated periodically to match current browser versions
_CHROME_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

# Browser launch arguments to reduce detectability
# These disable various automation indicators that websites can detect
_STEALTH_BROWSER_ARGS = [
    # Disable automation-related flags
    "--disable-blink-features=AutomationControlled",
    # Disable infobars that indicate automation
    "--disable-infobars",
    # Use a standard window size
    "--window-size=1920,1080",
    # Disable dev-shm usage for stability in containers
    "--disable-dev-shm-usage",
    # Disable GPU for headless stability
    "--disable-gpu",
    # Avoid sandbox issues in containers
    "--no-sandbox",
    # Disable setuid sandbox
    "--disable-setuid-sandbox",
    # Enable standard web features
    "--enable-features=NetworkService,NetworkServiceInProcess",
]


def get_random_user_agent() -> str:
    """Get a random realistic Chrome user agent string."""
    return random.choice(_CHROME_USER_AGENTS)


def get_stealth_launch_args() -> list[str]:
    """Get browser launch arguments for stealth mode."""
    return _STEALTH_BROWSER_ARGS.copy()


async def launch_stealth_browser(
    playwright: Playwright,
    *,
    headless: bool = True,
    extra_args: list[str] | None = None,
) -> Browser:
    """Launch a Chromium browser with stealth configuration.

    This uses rebrowser-playwright patches combined with launch arguments
    to minimize automation detection.

    Args:
        playwright: The Playwright instance to use.
        headless: Whether to run in headless mode. Defaults to True.
        extra_args: Additional browser arguments to include.

    Returns:
        A launched Browser instance with stealth configuration.
    """
    args = get_stealth_launch_args()
    if extra_args:
        args.extend(extra_args)

    logger.debug("Launching stealth browser with args: %s", args)

    browser = await playwright.chromium.launch(
        headless=headless,
        args=args,
    )
    return browser


async def create_stealth_context(
    browser: Browser,
    *,
    viewport: ViewportSize | None = None,
    user_agent: str | None = None,
    ignore_https_errors: bool = False,
    locale: str = "en-US",
    timezone_id: str | None = None,
) -> BrowserContext:
    """Create a browser context with stealth configuration.

    Args:
        browser: The browser instance to create context in.
        viewport: Optional viewport dimensions. Defaults to 1920x1080.
        user_agent: Optional user agent string. Uses random Chrome UA if not provided.
        ignore_https_errors: Whether to ignore HTTPS certificate errors.
        locale: Browser locale setting. Defaults to "en-US".
        timezone_id: IANA timezone ID (e.g., "America/New_York"). If None, uses
            system default.

    Returns:
        A BrowserContext configured for stealth operation.
    """
    effective_viewport: ViewportSize = (
        viewport if viewport is not None else {"width": 1920, "height": 1080}
    )

    if user_agent is None:
        user_agent = get_random_user_agent()

    logger.debug(
        "Creating stealth context with user_agent: %s", user_agent[:50] + "..."
    )

    context = await browser.new_context(
        viewport=effective_viewport,
        user_agent=user_agent,
        ignore_https_errors=ignore_https_errors,
        locale=locale,
        timezone_id=timezone_id,
        # Device scale factor for high-DPI displays
        device_scale_factor=1,
        # Enable JavaScript
        java_script_enabled=True,
        # Accept all permissions to avoid prompts
        permissions=["geolocation"],
    )
    return context
