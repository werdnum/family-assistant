"""Shared browser session state for browser automation tools.

Both the coordinate-based Computer Use tools (``computer_use.py``) and the
semantic DOM tools (``browser_dom.py``) run against the same underlying
Playwright browser instance. Extracting session management here lets the two
profiles share a tab when the first delegates to the second via
``delegate_to_service`` — the shared ``conversation_id`` keeps them pointed at
the same ``BrowserSession``.

The session is also the chokepoint every browser operation passes through: the
``@browser_operation`` decorator registers a tool as a browser operation and
runs its body under :meth:`BrowserSession.operation`, which holds the
conversation's browser lock and, within a batch of tool calls, waits for the
earlier browser siblings the model issued before it.
"""

from __future__ import annotations

import asyncio
import functools
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Concatenate

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
    from collections.abc import AsyncIterator, Awaitable, Callable

    from family_assistant.services.ucp import MerchantUCPProfile
    from family_assistant.tools.types import ToolExecutionContext, ToolResult

# Every tool that drives the shared browser, filled by ``@browser_operation``.
# One registry rather than a hand-maintained list, so a browser tool that
# forgets the decorator is simply not serialised rather than silently absent
# from a list someone has to remember to update.
BROWSER_TOOL_NAMES: set[str] = set()

# Default screen dimensions for coordinate-based Computer Use.
# The semantic DOM tools don't care about these values, but they still scope
# the Playwright viewport so visual screenshots (e.g. ``browser_screenshot``)
# render at a consistent size across both profiles.
SCREEN_WIDTH = 1024
SCREEN_HEIGHT = 768

# Refs are numbered from a counter seeded in this range when a conversation's
# browser state is created, so numbers issued before a process restart do not
# collide with numbers issued after it.
_REF_SEED_MIN = 1_000
_REF_SEED_MAX = 1_000_000


def _seed_next_ref() -> int:
    # Non-cryptographic randomness is the right tool: the seed keeps post-restart
    # numbers away from pre-restart ones, and nothing is authorized by a ref.
    return random.randrange(_REF_SEED_MIN, _REF_SEED_MAX)


@dataclass
class BrowserSession:
    """The conversation's persistent browser state, on both backend paths.

    On the local path it also owns the Playwright lifecycle: a single tab,
    lazily launched by ``ensure_page``. On the remote (browser-server) path the
    tab lives in the service and only the conversation-level state here
    applies — the ref counter, the UCP discovery cache, and the lock that
    serialises browser operations.

    ``next_ref`` is the lowest number the next snapshot may issue for a node it
    has not seen before. The walker takes it, never allocates below the highest
    number already stamped on the document, and reports the advanced counter
    back, so a ref names one node for the whole conversation.
    """

    playwright: Playwright | None = field(default=None, repr=False)
    browser: Browser | None = field(default=None, repr=False)
    context: BrowserContext | None = field(default=None, repr=False)
    page: Page | None = field(default=None, repr=False)
    screen_width: int = SCREEN_WIDTH
    screen_height: int = SCREEN_HEIGHT
    # IANA timezone (e.g. ``"Australia/Sydney"``) applied to the browser context so
    # in-page JS (``new Date()``, ``Intl``) reports the user's local time. ``None``
    # leaves the host default in place. Mirrors the remote browser-server backend.
    timezone_id: str | None = None
    # Lowest number the next snapshot may issue for a node it has not stamped
    # before. Randomly seeded so refs from before a restart cannot be reissued
    # for a different node after one.
    next_ref: int = field(default_factory=_seed_next_ref)
    # Serialises this conversation's browser operations, so each one runs
    # against the page as the previous one left it.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
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
                timezone_id=self.timezone_id,
            )
            self.context = context

        page = await context.new_page()
        self.page = page
        return page

    @asynccontextmanager
    async def operation(
        self, exec_context: ToolExecutionContext
    ) -> AsyncIterator[None]:
        """Hold the conversation's browser for one operation, in issue order.

        The model's tool calls run concurrently, so without this two browser
        actions from one response would race and the second could resolve its
        ref against a page the first had already replaced. Waiting for the
        earlier browser siblings of the same batch first, then taking the lock,
        makes every operation run against the page the previous one left.
        """
        batch = exec_context.tool_call_batch
        call_id = exec_context.tool_call_id
        if batch is not None and call_id is not None:
            await batch.wait_done([
                sibling_id
                for sibling_id, tool_name in batch.earlier(call_id)
                if tool_name in BROWSER_TOOL_NAMES
            ])
        async with self.lock:
            yield

    async def close(self) -> None:
        """Close all browser resources."""
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
        tz = getattr(exec_context, "timezone", None)
        _sessions[session_key] = BrowserSession(timezone_id=str(tz) if tz else None)
    return _sessions[session_key]


async def close_browser_session(exec_context: ToolExecutionContext) -> None:
    """Close and remove the browser session for the given execution context."""
    session_key = exec_context.conversation_id or "default"
    if session_key in _sessions:
        await _sessions[session_key].close()
        del _sessions[session_key]


def browser_operation[**P](
    func: Callable[Concatenate[ToolExecutionContext, P], Awaitable[ToolResult]],
) -> Callable[Concatenate[ToolExecutionContext, P], Awaitable[ToolResult]]:
    """Register a tool as a browser operation and serialise its body.

    The tool name is the function name without the ``_tool`` suffix, which is
    the naming convention every tool module follows. Registering here rather
    than in a hand-written list is what keeps the ordering rule and the set of
    tools it applies to from drifting apart.
    """
    BROWSER_TOOL_NAMES.add(func.__name__.removesuffix("_tool"))

    @functools.wraps(func)
    async def wrapper(
        exec_context: ToolExecutionContext, *args: P.args, **kwargs: P.kwargs
    ) -> ToolResult:
        session = await get_browser_session(exec_context)
        async with session.operation(exec_context):
            return await func(exec_context, *args, **kwargs)

    return wrapper


def denormalize_coordinate(value: int, max_value: int) -> int:
    """Convert normalized coordinate (0-1000) to pixel value."""
    return int(value / 1000 * max_value)
