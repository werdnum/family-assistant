"""Shared browser session state for browser automation tools.

Both the coordinate-based Computer Use tools (``computer_use.py``) and the
semantic DOM tools (``browser_dom.py``) run against the same underlying
Playwright browser instance. Extracting session management here lets the two
profiles share a tab when the first delegates to the second via
``delegate_to_service`` — the shared ``conversation_id`` keeps them pointed at
the same ``BrowserSession``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rebrowser_playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from family_assistant.utils.stealth_browser import (
    create_stealth_context,
    launch_stealth_browser,
)

if TYPE_CHECKING:
    from family_assistant.services.ucp import MerchantUCPProfile
    from family_assistant.tools.types import ToolExecutionContext

# Default screen dimensions for coordinate-based Computer Use.
# The semantic DOM tools don't care about these values, but they still scope
# the Playwright viewport so visual screenshots (e.g. ``browser_screenshot``)
# render at a consistent size across both profiles.
SCREEN_WIDTH = 1024
SCREEN_HEIGHT = 768


@dataclass
class BrowserSession:
    """Manages the browser lifecycle for browser automation tools.

    Each session owns a single Playwright-driven tab. The session is lazily
    initialized — ``ensure_page`` is what actually launches the browser.

    ``ref_cache`` is populated by the semantic DOM tools when they take an
    accessibility snapshot, mapping stable short refs (e.g. ``"e12"``) to
    serialized selectors so subsequent tool calls can resolve them back to
    Playwright locators. Coordinate-based tools don't touch it.
    """

    playwright: Playwright | None = field(default=None, repr=False)
    browser: Browser | None = field(default=None, repr=False)
    context: BrowserContext | None = field(default=None, repr=False)
    page: Page | None = field(default=None, repr=False)
    screen_width: int = SCREEN_WIDTH
    screen_height: int = SCREEN_HEIGHT
    # Maps short refs (e.g. ``"e12"``) to CSS selectors that the
    # semantic DOM tools can hand back to Playwright. Populated by
    # browser_dom snapshots; cleared on navigation.
    ref_cache: dict[str, str] = field(default_factory=dict)
    # Caches UCP discovery results keyed by origin (e.g.
    # ``"https://shop.example.com"``). Values are a ``MerchantUCPProfile`` or
    # ``None`` (negative cache) so repeated navigation within an origin probes
    # ``/.well-known/ucp`` at most once per session.
    ucp_profiles: dict[str, MerchantUCPProfile | None] = field(default_factory=dict)
    # Origin (or ``None`` for non-HTTPS) of the most recent snapshot. Used to
    # surface the UCP hint only when navigation changes origin, rather than
    # repeating it on every action against the same page.
    last_probed_origin: str | None = None

    async def ensure_page(self) -> Page:
        """Ensure a browser page is available, creating one if necessary."""
        if self.page is not None:
            return self.page

        if self.playwright is None:
            self.playwright = await async_playwright().start()

        if self.browser is None:
            self.browser = await launch_stealth_browser(self.playwright, headless=True)

        context = self.context
        if context is None:
            context = await create_stealth_context(
                self.browser,
                viewport={"width": self.screen_width, "height": self.screen_height},
            )
            self.context = context

        page = await context.new_page()
        self.page = page
        return page

    def clear_refs(self) -> None:
        """Invalidate the ref cache. Called on navigation to prevent stale refs."""
        self.ref_cache.clear()

    async def close(self) -> None:
        """Close all browser resources."""
        self.ref_cache.clear()

        if self.context is not None:
            await self.context.close()
            self.context = None
            self.page = None

        if self.browser is not None:
            await self.browser.close()
            self.browser = None

        if self.playwright is not None:
            await self.playwright.stop()
            self.playwright = None


# Session storage keyed by conversation_id for multi-user support.
_sessions: dict[str, BrowserSession] = {}


async def get_browser_session(exec_context: ToolExecutionContext) -> BrowserSession:
    """Get or create a browser session for the given execution context."""
    session_key = exec_context.conversation_id or "default"
    if session_key not in _sessions:
        _sessions[session_key] = BrowserSession()
    return _sessions[session_key]


async def close_browser_session(exec_context: ToolExecutionContext) -> None:
    """Close and remove the browser session for the given execution context."""
    session_key = exec_context.conversation_id or "default"
    if session_key in _sessions:
        await _sessions[session_key].close()
        del _sessions[session_key]


def denormalize_coordinate(value: int, max_value: int) -> int:
    """Convert normalized coordinate (0-1000) to pixel value."""
    return int(value / 1000 * max_value)
