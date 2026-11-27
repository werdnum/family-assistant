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
import logging
import math
import random
import struct
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Concatenate, Literal

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
    from collections.abc import Awaitable, Callable

    from family_assistant.services.browser_live_view import (
        BrowserBackendKind,
        BrowserLiveViewPublisher,
    )
    from family_assistant.services.ucp import MerchantUCPProfile
    from family_assistant.tools.browser_webmcp import WebMCPToolDescriptor
    from family_assistant.tools.types import ToolExecutionContext, ToolResult

logger = logging.getLogger(__name__)

# Tools whose post-call state must not trigger a live-view screenshot (handoff
# transfers the remote browser lease to the human; login outcome is metadata).
_LIVE_VIEW_SKIP_TOOLS = frozenset({
    "browser_request_handoff",
    "browser_report_login_outcome",
})
_LIVE_VIEW_SCREENSHOT_TIMEOUT_SECONDS = 5.0

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

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_KEY_NAME_MAPPING: dict[str, str] = {
    "ctrl": "Control",
    "control": "Control",
    "controlormeta": "ControlOrMeta",
    "alt": "Alt",
    "option": "Alt",
    "shift": "Shift",
    "meta": "Meta",
    "cmd": "Meta",
    "command": "Meta",
    "win": "Meta",
    "super": "Meta",
    "enter": "Enter",
    "return": "Enter",
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "space": "Space",
    "backspace": "Backspace",
    "delete": "Delete",
    "del": "Delete",
    "insert": "Insert",
    "home": "Home",
    "end": "End",
    "pageup": "PageUp",
    "page_up": "PageUp",
    "pagedown": "PageDown",
    "page_down": "PageDown",
    "up": "ArrowUp",
    "arrowup": "ArrowUp",
    "arrow_up": "ArrowUp",
    "down": "ArrowDown",
    "arrowdown": "ArrowDown",
    "arrow_down": "ArrowDown",
    "left": "ArrowLeft",
    "arrowleft": "ArrowLeft",
    "arrow_left": "ArrowLeft",
    "right": "ArrowRight",
    "arrowright": "ArrowRight",
    "arrow_right": "ArrowRight",
}
_TRANSFER_KEYS = frozenset({"c", "x", "v", "insert", "delete"})
_TRANSFER_MODIFIERS = frozenset({"control", "meta", "controlormeta"})


@dataclass(frozen=True, slots=True)
class ScreenshotGeometry:
    """Coordinate-space metadata captured alongside a browser screenshot."""

    url: str
    image_width: int
    image_height: int
    viewport_width: float
    viewport_height: float
    device_pixel_ratio: float
    scroll_x: float
    scroll_y: float
    scroll_known: bool = True


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
    # Digest and descriptors of the most recent snapshot's WebMCP tools, so
    # full schemas are rendered once per tool-set change rather than repeated
    # on every turn.
    last_webmcp_fingerprint: str | None = None
    last_webmcp_tools: dict[str, WebMCPToolDescriptor] = field(default_factory=dict)
    # Coordinate-space geometry of the most recent ``browser_screenshot`` on
    # this session (cleared on navigation or scroll so coordinate actions do
    # not act on a stale frame).
    last_screenshot: ScreenshotGeometry | None = None
    last_screenshot_invalidation_reason: str | None = None

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


# Browser profiles governed by upfront brief review (clean-room browsing).
BROWSER_BRIEF_REVIEW_PROFILES = frozenset({
    "browser_profile",
    "browser_visual_profile",
})

BrowserSessionReviewMode = Literal["checked", "unchecked"]

# Session storage keyed by browser scope (see ``browser_scope_key``).
_sessions: dict[str, BrowserSession] = {}

# Monotonic in-memory review mode per browser scope (unchecked -> checked only).
# Unknown scopes fail closed to ``checked``.
_session_review_modes: dict[str, BrowserSessionReviewMode] = {}

# Browser scopes where a human has completed a handback to the agent. Tracked
# at scope level so the handed-back posture survives across turns in the same
# browser session and prevents later snapshots from being stamped as public.
_handed_back_scopes: set[str] = set()

# The browser scope of each delegated subconversation. Queued delegations are
# resolved from their run row and memoized here; inline delegations have no run
# row, so ``delegated_browser_scope`` registers them for the child's lifetime.
_subconversation_scopes: dict[str, str] = {}


def get_browser_session_review_mode(scope: str) -> BrowserSessionReviewMode:
    """Return the active review mode for ``scope``, failing closed to ``checked``."""
    if scope in _handed_back_scopes:
        return "checked"
    return _session_review_modes.get(scope, "checked")


def has_recorded_browser_session_review_mode(scope: str) -> bool:
    """Return whether ``scope`` has an explicit recorded brief-review mode."""
    return scope in _session_review_modes


def initialize_browser_session_review_mode(
    scope: str, mode: BrowserSessionReviewMode
) -> BrowserSessionReviewMode:
    """Initialize an initial delegation's browser scope review mode monotonically.

    A scope that is already ``checked`` or handed back can never move back to
    ``unchecked``.
    """
    if scope in _handed_back_scopes or _session_review_modes.get(scope) == "checked":
        _session_review_modes[scope] = "checked"
        return "checked"
    _session_review_modes[scope] = mode
    return mode


def mark_browser_scope_checked(scope: str) -> None:
    """Permanently latch ``scope`` into ``checked`` mode."""
    _session_review_modes[scope] = "checked"


def is_browser_scope_unchecked(scope: str) -> bool:
    """Return whether ``scope`` is currently authorized to run in ``unchecked`` mode."""
    return (
        scope not in _handed_back_scopes
        and _session_review_modes.get(scope) == "unchecked"
    )


def clear_browser_session_review_modes() -> None:
    """Clear in-memory browser session review modes (for test isolation)."""
    _session_review_modes.clear()


def mark_browser_scope_handed_back(scope: str) -> None:
    """Record that a human handed this browser scope back to the agent."""
    _handed_back_scopes.add(scope)
    _session_review_modes[scope] = "checked"


def is_browser_scope_handed_back(scope: str) -> bool:
    """Return whether a human previously handed this browser scope back."""
    return scope in _handed_back_scopes


def clear_handed_back_scopes() -> None:
    """Clear tracked handed-back browser scopes (for test isolation)."""
    _handed_back_scopes.clear()


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


async def continues_parent_browser_tab(
    exec_context: ToolExecutionContext,
    source_service: object,
    source_profile_id: str | None,
    target_profile_id: str,
) -> bool:
    """Whether a delegated turn shares its parent's browser tab and scope."""
    return await _continues_parent_tab(
        exec_context,
        source_service,
        source_profile_id,
        target_profile_id,
    )


async def _continues_parent_tab(
    exec_context: ToolExecutionContext,
    source_service: object,
    source_profile_id: str | None,
    target_profile_id: str,
) -> bool:
    """Whether a delegated turn works in its parent's tab rather than its own.

    Only when the parent drives the browser, the child is a different profile
    (a hop such as the visual one), and both sit on the same side of the
    credential-mode split, since ordinary and credential browsing never share a
    browser context. A profile delegating to itself is fanning work out, and
    its runs go in parallel, so each gets a tab of its own.
    """
    if source_profile_id == target_profile_id:
        return False
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
    _handed_back_scopes.discard(session_key)
    _session_review_modes.pop(session_key, None)
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
                try:
                    await _capture_and_publish_live_frame(exec_context, name, result)
                except Exception:
                    logger.debug(
                        "Skipping live browser frame publish after %s",
                        name,
                        exc_info=True,
                    )
                return result

        # The marker a test reads to check the registry against the tool table.
        wrapper.browser_tool_name = name  # pyright: ignore[reportAttributeAccessIssue]
        return wrapper

    return decorate


def _extract_result_screenshot(result: ToolResult) -> tuple[bytes, str] | None:
    """Return ``(bytes, mime_type)`` if the tool result already carries an image."""
    for attachment in result.attachments or ():
        if isinstance(attachment.content, bytes) and attachment.mime_type.startswith(
            "image/"
        ):
            return attachment.content, attachment.mime_type
    return None


def _has_later_browser_sibling(exec_context: ToolExecutionContext) -> bool:
    """Whether another browser tool call follows this one in the same batch."""
    batch = exec_context.tool_call_batch
    call_id = exec_context.tool_call_id
    if batch is None or call_id is None:
        return False
    return any(tool_name in BROWSER_TOOL_NAMES for _, tool_name in batch.later(call_id))


def _live_view_targets(
    exec_context: ToolExecutionContext,
    scope: str,
) -> list[tuple[str, str | None, str | None]]:
    """Return ``(conversation_id, subconversation_id, turn_id)`` targets for a frame.

    When a delegated browser turn continues its parent's tab (for example,
    ``browser_profile`` delegating a visual step to ``browser_visual_profile``),
    both the child subconversation and the tab-owning parent receive the frame
    so users watching either surface stay in sync.
    """
    conversation_id = exec_context.conversation_id
    if not conversation_id:
        return []
    subconversation_id = getattr(exec_context, "subconversation_id", None)
    targets: list[tuple[str, str | None, str | None]] = [
        (conversation_id, subconversation_id, exec_context.turn_id)
    ]
    if subconversation_id is not None:
        if scope == conversation_id:
            targets.append((conversation_id, None, None))
        elif scope.startswith(f"{conversation_id}/"):
            owner_sub = scope.removeprefix(f"{conversation_id}/")
            if owner_sub and owner_sub != subconversation_id:
                targets.append((conversation_id, owner_sub, None))
    return targets


async def _capture_and_publish_live_frame(
    exec_context: ToolExecutionContext,
    tool_name: str,
    result: ToolResult,
) -> None:
    """Best-effort publish of the latest browser screenshot to live subscribers."""
    service = getattr(exec_context, "processing_service", None)
    publisher: BrowserLiveViewPublisher | None = (
        getattr(service, "browser_live_view_publisher", None)
        if service is not None
        else None
    )
    if publisher is None or tool_name in _LIVE_VIEW_SKIP_TOOLS:
        return

    data = result.get_data()
    if isinstance(data, dict) and data.get("error"):
        return

    from family_assistant.tools.browser_backend import (  # noqa: PLC0415 - backend depends on session types
        get_existing_browser_backend,
        infer_browser_backend_kind,
    )

    existing_image = _extract_result_screenshot(result)
    backend = await get_existing_browser_backend(exec_context)

    if existing_image is not None:
        image_bytes, mime_type = existing_image
    else:
        if backend is None or _has_later_browser_sibling(exec_context):
            return
        image_bytes = await asyncio.wait_for(
            backend.screenshot_png(),
            timeout=_LIVE_VIEW_SCREENSHOT_TIMEOUT_SECONDS,
        )
        mime_type = "image/png"

    if not image_bytes:
        return

    raw_url = data.get("url") if isinstance(data, dict) else None
    url = (
        str(raw_url)
        if isinstance(raw_url, str) and raw_url
        else (backend.current_url if backend is not None else "")
    )
    backend_kind: BrowserBackendKind = (
        backend.backend_kind
        if backend is not None
        else infer_browser_backend_kind(exec_context)
    )
    width = backend.screen_width if backend is not None else None
    height = backend.screen_height if backend is not None else None

    scope = await browser_scope_key(exec_context)
    for conv_id, sub_id, turn_id in _live_view_targets(exec_context, scope):
        await publisher.publish_browser_frame(
            conversation_id=conv_id,
            subconversation_id=sub_id,
            turn_id=turn_id,
            image_bytes=image_bytes,
            mime_type=mime_type,
            url=url,
            tool_name=tool_name,
            backend_kind=backend_kind,
            width=width,
            height=height,
        )


def denormalize_coordinate(value: int, max_value: int) -> int:
    """Convert normalized coordinate (0-1000) to pixel value."""
    return int(value / 1000 * max_value)


def parse_png_dimensions(png: bytes) -> tuple[int, int]:
    """Return ``(width, height)`` in pixels from a PNG's IHDR chunk."""
    if len(png) < 24 or png[:8] != _PNG_SIGNATURE or png[12:16] != b"IHDR":
        raise ValueError("Screenshot bytes are not a valid PNG image")
    width, height = struct.unpack(">II", png[16:24])
    if width <= 0 or height <= 0:
        raise ValueError(f"Invalid PNG dimensions: {width}x{height}")
    return width, height


def map_screenshot_point_to_viewport(
    x: float, y: float, geometry: ScreenshotGeometry
) -> tuple[float, float]:
    """Map screenshot pixel coordinates ``(x, y)`` to viewport CSS pixels.

    Playwright ``page.screenshot(full_page=False)`` renders the current
    viewport at ``devicePixelRatio`` into an ``image_width x image_height``
    PNG, while ``page.mouse`` expects viewport-relative CSS coordinates in
    ``[0, viewport_width) x [0, viewport_height)``. Scroll offset is already
    reflected in the viewport capture and is not added here.
    """
    if not (math.isfinite(x) and math.isfinite(y)):
        raise ValueError(f"Coordinates must be finite numbers, got ({x!r}, {y!r}).")
    if x < 0 or x > geometry.image_width or y < 0 or y > geometry.image_height:
        raise ValueError(
            f"Coordinates ({x:g}, {y:g}) are outside the last screenshot "
            f"({geometry.image_width}x{geometry.image_height} px; valid range "
            f"x: 0..{geometry.image_width - 1}, y: 0..{geometry.image_height - 1})."
        )
    css_x = x * (geometry.viewport_width / geometry.image_width)
    css_y = y * (geometry.viewport_height / geometry.image_height)
    max_x = max(0.0, geometry.viewport_width - 1e-3)
    max_y = max(0.0, geometry.viewport_height - 1e-3)
    return min(css_x, max_x), min(css_y, max_y)


def _map_single_key_name(key: str) -> str:
    """Map a single model-emitted key name to its Playwright key name."""
    key_lower = key.lower()
    if len(key_lower) == 1:
        return key
    if key_lower in _KEY_NAME_MAPPING:
        return _KEY_NAME_MAPPING[key_lower]
    if key_lower.startswith("f") and len(key_lower) <= 3:
        try:
            num = int(key_lower[1:])
        except ValueError:
            num = 0
        if 1 <= num <= 12:
            return f"F{num}"
    return key[0].upper() + key[1:] if key else key


def map_key_name(key: str) -> str:
    """Map a key or ``+``-joined key chord to Playwright key names."""
    stripped = key.strip()
    if not stripped:
        return ""
    if len(stripped) > 1 and "+" in stripped:
        parts = [part.strip() for part in stripped.split("+") if part.strip()]
        if parts:
            return "+".join(_map_single_key_name(part) for part in parts)
    return _map_single_key_name(stripped)


def is_transfer_key_chord(key: str) -> bool:
    """Return whether ``key`` is a clipboard/transfer chord (Ctrl/Cmd+C/X/V/Insert, Shift+Insert/Delete)."""
    parts = [part.strip().lower() for part in key.split("+") if part.strip()]
    if not parts:
        return False
    base, modifiers = parts[-1], set(parts[:-1])
    if base in {"keyc", "keyx", "keyv"}:
        base = base[-1]
    if base not in _TRANSFER_KEYS:
        return False
    if modifiers & _TRANSFER_MODIFIERS:
        return True
    return base in {"insert", "delete"} and "shift" in modifiers
