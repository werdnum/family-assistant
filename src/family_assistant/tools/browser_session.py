"""Shared browser session state for browser automation tools.

Both the coordinate-based Computer Use tools (``computer_use.py``) and the
semantic DOM tools (``browser_dom.py``) run against the same underlying
Playwright browser instance. Extracting session management here lets the two
profiles share a tab when the first delegates to the second via
``delegate_to_service``.

Browser state is keyed by *browser scope* (see :func:`browser_scope_key`), not by
conversation: each delegated browser run gets its own tab, so two runs the main
assistant starts at once do not drive the same page, while a turn delegated by a
profile that itself drives the browser (the semantic-to-visual hop) stays in its
parent's tab.

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
    from collections.abc import AsyncGenerator, Awaitable, Callable

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
    ) -> AsyncGenerator[None]:
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


# Session storage keyed by browser scope (see ``browser_scope_key``).
_sessions: dict[str, BrowserSession] = {}

# The browser scope of each delegated subconversation. Queued delegations are
# resolved from their run row and memoized here; inline delegations have no run
# row, so ``delegated_browser_scope`` registers them for the child's lifetime.
_subconversation_scopes: dict[str, str] = {}


async def browser_scope_key(exec_context: ToolExecutionContext) -> str:
    """The key this turn's browser state (tab, lock, refs) lives under.

    The top-level conversation turn uses the conversation's own scope. A
    delegated turn inherits its parent's scope when the parent profile drives
    the browser itself in the same credential mode -- that is how the visual
    hop works in the semantic profile's tab -- and otherwise gets a scope of its
    own, so parallel browser runs each have their own tab. A credential child of
    an ordinary browser run never shares its parent's browser anyway, so it is
    scoped to itself too, and parallel credential runs stay apart.
    """
    conversation_id = exec_context.conversation_id or "default"
    subconversation_id = getattr(exec_context, "subconversation_id", None)
    if subconversation_id is None:
        return conversation_id
    return await _subconversation_scope(
        exec_context, conversation_id, subconversation_id
    )


async def _subconversation_scope(
    exec_context: ToolExecutionContext,
    conversation_id: str,
    subconversation_id: str,
) -> str:
    scope = _subconversation_scopes.get(subconversation_id)
    if scope is not None:
        return scope
    scope = await _inherited_scope(exec_context, conversation_id, subconversation_id)
    if scope is None:
        scope = f"{conversation_id}/{subconversation_id}"
    _subconversation_scopes[subconversation_id] = scope
    return scope


async def _inherited_scope(
    exec_context: ToolExecutionContext,
    conversation_id: str,
    subconversation_id: str,
) -> str | None:
    """The parent's scope, when this run continues its parent's tab."""
    service = getattr(exec_context, "processing_service", None)
    registry = getattr(service, "processing_services_registry", None)
    if registry is None:
        return None
    run = await exec_context.db_context.delegation_runs.get_by_subconversation_id(
        subconversation_id
    )
    if run is None:
        return None
    if not await _continues_parent_tab(
        exec_context,
        registry.get(run["source_profile_id"]),
        run["source_profile_id"],
        run["target_service_id"],
    ):
        return None
    parent = run["source_subconversation_id"]
    if parent is None:
        return conversation_id
    return await _subconversation_scope(exec_context, conversation_id, parent)


async def _continues_parent_tab(
    exec_context: ToolExecutionContext,
    source_service: object,
    source_profile_id: str | None,
    target_profile_id: str,
) -> bool:
    """Whether a delegated turn works in its parent's tab rather than its own.

    Only when the parent drives the browser and both sit on the same side of
    the credential-mode split, since ordinary and credential browsing never
    share a browser context.
    """
    service = getattr(exec_context, "processing_service", None)
    app_config = getattr(service, "app_config", None)
    handoff = getattr(app_config, "browser_handoff_config", None)
    credential_profiles = (
        handoff.autofill_capable_profiles if handoff is not None else []
    )
    if (source_profile_id in credential_profiles) != (
        target_profile_id in credential_profiles
    ):
        return False
    return await _drives_browser(source_service)


async def _drives_browser(service: object) -> bool:
    """Whether a profile has browser tools of its own to share a tab from."""
    tools_provider = getattr(service, "tools_provider", None)
    if tools_provider is None:
        return False
    from family_assistant.tools.infrastructure import (  # noqa: PLC0415 - infrastructure imports the tools package
        get_tool_definitions_for_advertisement,
    )

    advertised = await get_tool_definitions_for_advertisement(
        tools_provider, can_confirm=True
    )
    return any(
        definition.get("function", {}).get("name") in BROWSER_TOOL_NAMES
        for definition in advertised
    )


@asynccontextmanager
async def delegated_browser_scope(
    exec_context: ToolExecutionContext,
    subconversation_id: str,
    target_profile_id: str,
) -> AsyncGenerator[None]:
    """Give an inline delegated turn its browser scope for its lifetime.

    The inline path mints a subconversation with no run row, so the child could
    not find its parent's scope by itself.
    """
    if await _continues_parent_tab(
        exec_context,
        getattr(exec_context, "processing_service", None),
        getattr(exec_context, "processing_profile_id", None),
        target_profile_id,
    ):
        scope = await browser_scope_key(exec_context)
    else:
        scope = f"{exec_context.conversation_id or 'default'}/{subconversation_id}"
    _subconversation_scopes[subconversation_id] = scope
    try:
        yield
    finally:
        _subconversation_scopes.pop(subconversation_id, None)


async def get_browser_session(exec_context: ToolExecutionContext) -> BrowserSession:
    """Get or create the browser session for this turn's browser scope."""
    session_key = await browser_scope_key(exec_context)
    if session_key not in _sessions:
        tz = getattr(exec_context, "timezone", None)
        _sessions[session_key] = BrowserSession(timezone_id=str(tz) if tz else None)
    return _sessions[session_key]


async def close_browser_session(exec_context: ToolExecutionContext) -> None:
    """Close and remove the browser session for this turn's browser scope."""
    session_key = await browser_scope_key(exec_context)
    if session_key in _sessions:
        await _sessions[session_key].close()
        del _sessions[session_key]


def browser_operation[**P](
    tool_name: str | None = None,
) -> Callable[
    [Callable[Concatenate[ToolExecutionContext, P], Awaitable[ToolResult]]],
    Callable[Concatenate[ToolExecutionContext, P], Awaitable[ToolResult]],
]:
    """Register a tool as a browser operation and serialise its body.

    ``tool_name`` is the name the tool is exposed under, which is what a
    :class:`~family_assistant.tools.types.ToolCallBatch` carries; it defaults to
    the function name without the ``_tool`` suffix. The wrapper is marked so a
    test can check the registry against the tool table, which is what keeps the
    ordering rule and the set of tools it applies to from drifting apart.
    """

    def decorate(
        func: Callable[Concatenate[ToolExecutionContext, P], Awaitable[ToolResult]],
    ) -> Callable[Concatenate[ToolExecutionContext, P], Awaitable[ToolResult]]:
        name = tool_name or func.__name__.removesuffix("_tool")
        BROWSER_TOOL_NAMES.add(name)

        @functools.wraps(func)
        async def wrapper(
            exec_context: ToolExecutionContext, *args: P.args, **kwargs: P.kwargs
        ) -> ToolResult:
            session = await get_browser_session(exec_context)
            async with session.operation(exec_context):
                from family_assistant.tools.browser_backend import (  # noqa: PLC0415 - backend depends on session types
                    authenticated_binding_for,
                )

                binding = authenticated_binding_for(exec_context.subconversation_id)
                try:
                    result = await func(exec_context, *args, **kwargs)
                except Exception:
                    if binding is not None:
                        binding.operation_failures.add(name)
                    raise
                if binding is not None:
                    data = result.get_data()
                    if isinstance(data, dict) and data.get("error"):
                        binding.operation_failures.add(name)
                    else:
                        binding.operation_failures.discard(name)
                return result

        # The marker a test reads to check the registry against the tool table.
        wrapper.browser_tool_name = name  # pyright: ignore[reportAttributeAccessIssue]
        return wrapper

    return decorate


def denormalize_coordinate(value: int, max_value: int) -> int:
    """Convert normalized coordinate (0-1000) to pixel value."""
    return int(value / 1000 * max_value)
