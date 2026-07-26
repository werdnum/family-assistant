"""Semantic DOM browser automation tools.

These tools drive a Playwright browser session via the accessibility tree
rather than pixel coordinates, adopting AXI-style conventions:

- Every interactive element is assigned a stable short ref (``e1``, ``e2``, …)
  by tagging it with ``data-ref`` in the page.
- Snapshots are rendered as indented TOON-style text (cheaper to tokenize than
  JSON) and returned alongside structured ``data`` for tests.
- ``browser_exec`` is the escape hatch: it runs arbitrary JavaScript in the
  page via ``page.evaluate``. This is deliberately limited to in-page script
  execution — the JS runs inside V8 with only same-origin privileges and has
  no access to the Python process, browser internals, or other tabs.

The session (``BrowserSession``) is shared with :mod:`computer_use` via the
:mod:`browser_session` module, keyed on ``conversation_id``. A profile using
these tools can delegate to the visual profile and the other side will pick up
the same live tab.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

import httpx
import toons

from family_assistant.services.ucp import (
    MerchantUCPProfile,
    discover_merchant_ucp_profile,
    merchant_origin,
)
from family_assistant.tools.browser_backend import (
    BrowserBackend,
    BrowserBackendError,
    HandoffUnavailableError,
    get_browser_backend,
)
from family_assistant.tools.browser_session import get_browser_session
from family_assistant.tools.types import ToolAttachment, ToolDefinition, ToolResult
from family_assistant.utils.scraping import convert_html_bytes_to_markdown

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext

UCP_PROBE_TIMEOUT_SECONDS = 5.0
# Capability suffixes are merchant-controlled; only well-formed, bounded names
# are echoed into the assistant-authored hint to prevent instruction injection.
_SAFE_CAPABILITY_SUFFIX = re.compile(r"[a-z0-9_.-]{1,40}")
_MAX_HINT_CAPABILITIES = 12


class SnapshotNode(TypedDict):
    """A single element in the accessibility snapshot tree.

    Populated by the in-page JS walker. ``ref`` is the short id used by tool
    callers (``e12``); ``role`` and ``name`` drive the TOON rendering; the
    remaining fields are only present for elements that carry them (links,
    inputs, selects, …).
    """

    ref: str
    role: str
    name: str
    href: NotRequired[str]
    value: NotRequired[str]
    tag: NotRequired[str]
    input_type: NotRequired[str]
    children: NotRequired[list[SnapshotNode]]


class Snapshot(TypedDict):
    """Top-level accessibility snapshot returned by the in-page JS walker."""

    url: str
    title: str
    forms: int
    elements: int
    roots: list[SnapshotNode]


class SnapshotCounts(TypedDict):
    forms: int
    elements: int


class SnapshotData(TypedDict):
    """Structured data returned by ``_take_snapshot`` (fed to ``ToolResult.data``)."""

    text: str
    url: str
    title: str
    counts: SnapshotCounts
    refs: list[str]
    roots: list[SnapshotNode]


logger = logging.getLogger(__name__)

__all__ = [
    "BROWSER_DOM_TOOLS_DEFINITION",
    "browser_claim_handback_tool",
    "browser_click_tool",
    "browser_exec_tool",
    "browser_extract_tool",
    "browser_fill_tool",
    "browser_open_tool",
    "browser_request_handoff_tool",
    "browser_screenshot_tool",
    "browser_select_tool",
    "browser_snapshot_tool",
    "browser_wait_tool",
]


def _node_matches(node: SnapshotNode, query: str) -> bool:
    """Case-insensitive match against a node's role, name, and href."""
    needle = query.lower()
    return (
        needle in node["name"].lower()
        or needle in node["role"].lower()
        or needle in node.get("href", "").lower()
    )


def _any_match(nodes: list[SnapshotNode], query: str | None) -> bool:
    if not query:
        return True
    for node in nodes:
        if _node_matches(node, query) or _any_match(node.get("children", []), query):
            return True
    return False


def _filter_tree(
    nodes: list[SnapshotNode], query: str, *, include_all: bool
) -> list[SnapshotNode]:
    """Prune the snapshot tree to branches matching ``query``.

    A node is kept if it directly matches or any descendant matches. Once a
    node directly matches, its entire subtree survives — users filtering by
    "search" want the whole search form, not just the parts that spell
    "search".
    """
    kept: list[SnapshotNode] = []
    for node in nodes:
        children = node.get("children", [])
        node_matches = include_all or _node_matches(node, query)
        if not (node_matches or _any_match(children, query)):
            continue
        # TypedDicts can't be copy-constructed via dict(td) per pyright; the
        # shape is invariant, so a shallow cast preserves types safely.
        copy = cast("SnapshotNode", cast("object", dict(node)))
        if children:
            copy["children"] = _filter_tree(children, query, include_all=node_matches)
            if not copy["children"]:
                del copy["children"]
        else:
            copy.pop("children", None)
        kept.append(copy)
    return kept


def _format_toon(snapshot: Snapshot, query: str | None = None) -> str:
    """Render an accessibility snapshot as TOON v3 text via the ``toons`` lib.

    The snapshot is a plain nested dict, so ``toons.dumps`` handles the
    indentation, key:value lines, and tabular-array compaction where
    applicable. When ``query`` is supplied the tree is pre-pruned.
    """
    roots = snapshot["roots"]
    if query:
        roots = _filter_tree(roots, query, include_all=False)
    payload: dict[str, object] = {
        "url": snapshot["url"],
        "title": snapshot["title"],
        "forms": snapshot["forms"],
        "elements": snapshot["elements"],
        "roots": roots,
    }
    if query and not roots:
        payload["note"] = f"no matches for query={query!r}"
    return toons.dumps(payload)


def _collect_refs(
    nodes: list[SnapshotNode], out: dict[str, str] | None = None
) -> dict[str, str]:
    """Walk a snapshot tree, returning ``{ref: selector}`` for every node."""
    if out is None:
        out = {}
    for node in nodes:
        out[node["ref"]] = f'[data-fa-ref="{node["ref"]}"]'
        children = node.get("children")
        if children:
            _collect_refs(children, out)
    return out


async def _take_snapshot(backend: BrowserBackend, query: str | None) -> SnapshotData:
    """Capture a snapshot via the backend, update the ref cache, and return it."""
    snapshot = await backend.raw_snapshot()
    backend.ref_cache.clear()
    backend.ref_cache.update(_collect_refs(snapshot["roots"]))
    text = _format_toon(snapshot, query=query)
    return SnapshotData(
        text=text,
        url=snapshot["url"] or backend.current_url,
        title=snapshot["title"],
        counts=SnapshotCounts(forms=snapshot["forms"], elements=snapshot["elements"]),
        refs=list(backend.ref_cache.keys()),
        roots=snapshot["roots"],
    )


def _resolve_ref(backend: BrowserBackend, ref: str) -> str:
    """Return the selector for ``ref`` or raise a clear error."""
    selector = backend.ref_cache.get(ref)
    if selector is None:
        raise ValueError(
            f"Unknown ref {ref!r}. Refs are only valid for the most recent "
            f"snapshot; call browser_snapshot again after navigation or DOM "
            f"changes. Known refs: {sorted(backend.ref_cache.keys())[:10]}…"
        )
    return selector


def _format_ucp_hint(profile: MerchantUCPProfile) -> str:
    """Render a one-line hint telling the model this origin supports UCP.

    Capability names come from the (untrusted) merchant profile and are spliced
    into assistant-authored text, so only well-formed suffixes are kept and the
    list is bounded — a malicious key containing newlines or instruction text
    cannot leak into the hint as if it were assistant content.
    """
    shopping_capabilities: list[str] = []
    for name in profile.capability_names:
        if not name.startswith("dev.ucp.shopping."):
            continue
        suffix = name.removeprefix("dev.ucp.shopping.")
        if _SAFE_CAPABILITY_SUFFIX.fullmatch(suffix):
            shopping_capabilities.append(suffix)
        if len(shopping_capabilities) >= _MAX_HINT_CAPABILITIES:
            break
    capability_text = (
        f" Capabilities: {', '.join(shopping_capabilities)}."
        if shopping_capabilities
        else ""
    )
    return (
        f"🛒 This site supports UCP shopping at {profile.origin}.{capability_text} "
        f"Use ucp_add_to_cart / ucp_get_cart / ucp_transfer_checkout_to_human with "
        f'business_url="{profile.origin}".'
    )


async def _probe_ucp_support(
    exec_context: ToolExecutionContext, current_url: str | None
) -> str | None:
    """Probe the current origin's UCP profile, returning a hint when shoppable.

    Results are cached per browser session keyed by origin so repeated
    navigation within an origin probes ``/.well-known/ucp`` at most once.
    Returns ``None`` for non-HTTPS origins or sites without UCP shopping.

    The probe is a read-only GET to a fixed, reserved metadata path
    (``/.well-known/ucp``) over HTTPS, and the response is never returned to the
    page — only a sanitized "shoppable" hint reaches the model — so it is not
    treated as an SSRF risk worth a private-address guard.
    """
    origin = merchant_origin(current_url or "")
    if origin is None:
        return None

    # Same trusted suffixes gate both discovery redirects and endpoint hinting,
    # so the probe follows a redirect to a trusted platform host (e.g. a Shopify
    # store's *.myshopify.com shop host) exactly when it would hint the endpoint
    # found there.
    trusted_suffixes = _trusted_endpoint_suffixes(exec_context)
    session = await get_browser_session(exec_context)
    if origin in session.ucp_profiles:
        profile = session.ucp_profiles[origin]
    else:
        async with httpx.AsyncClient(timeout=UCP_PROBE_TIMEOUT_SECONDS) as client:
            # Pass the timeout explicitly so it bounds the whole redirect chain,
            # not just each hop: relying on the client-level timeout alone would
            # let a same-origin redirect chain stall the probe for up to
            # (MAX_DISCOVERY_REDIRECTS + 1) * UCP_PROBE_TIMEOUT_SECONDS.
            profile = await discover_merchant_ucp_profile(
                origin,
                client=client,
                timeout=UCP_PROBE_TIMEOUT_SECONDS,
                trusted_suffixes=trusted_suffixes,
            )
        session.ucp_profiles[origin] = profile

    # Only hint when the profile advertises an endpoint the shopping tools will
    # actually use (same-origin, same-site, or a trusted platform suffix); a
    # profile whose only binding is an untrusted cross-host endpoint is not
    # usable for this origin, so hinting it would mislead the model.
    if (
        profile is not None
        and profile.usable_shopping_endpoint(trusted_suffixes=trusted_suffixes)
        is not None
    ):
        return _format_ucp_hint(profile)
    return None


def _trusted_endpoint_suffixes(
    exec_context: ToolExecutionContext,
) -> tuple[str, ...]:
    """Trusted shopping-endpoint suffixes from config, or ``()`` if unavailable.

    The probe runs from snapshot paths that may lack a fully wired processing
    service; falling back to an empty tuple keeps it to same-origin/same-site
    hinting rather than raising.
    """
    service = getattr(exec_context, "processing_service", None)
    app_config = getattr(service, "app_config", None) if service is not None else None
    if app_config is None:
        return ()
    return tuple(app_config.ucp_config.trusted_endpoint_suffixes)


async def _ucp_hint_on_url_change(
    exec_context: ToolExecutionContext, current_url: str | None
) -> str | None:
    """Return a UCP hint only when the snapshot origin changed since the last.

    Detection is folded into the shared snapshot path and keyed on the current
    origin, so the hint surfaces once when navigation lands on a new origin
    rather than repeating on every action against the same page.
    """
    session = await get_browser_session(exec_context)
    origin = merchant_origin(current_url or "")
    if origin == session.last_probed_origin:
        return None
    session.last_probed_origin = origin
    if origin is None:
        return None
    return await _probe_ucp_support(exec_context, current_url)


async def _snapshot_result(
    exec_context: ToolExecutionContext,
    backend: BrowserBackend,
    *,
    query: str | None = None,
) -> ToolResult:
    """Take a snapshot and append a UCP hint when navigation reaches a new
    shopping-capable origin.

    Shared by every snapshot-returning navigation tool so UCP auto-detection
    fires after click-driven navigation, not just ``browser_open``, and only
    when the URL's origin changes from one snapshot to the next.
    """
    snap = await _take_snapshot(backend, query=query)
    text = snap["text"]
    ucp_hint = await _ucp_hint_on_url_change(exec_context, snap["url"])
    if ucp_hint is not None:
        text = f"{text}\n\n{ucp_hint}"
    return ToolResult(text=text, data=dict(snap))


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def browser_open_tool(
    exec_context: ToolExecutionContext, url: str, query: str | None = None
) -> ToolResult:
    """Navigate to a URL and return a snapshot in one call."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    backend = await get_browser_backend(exec_context)
    logger.info("browser_open: %s", url)
    await backend.goto(url)
    await backend.settle()
    return await _snapshot_result(exec_context, backend, query=query)


async def browser_snapshot_tool(
    exec_context: ToolExecutionContext, query: str | None = None
) -> ToolResult:
    """Re-capture an accessibility snapshot of the current page."""
    backend = await get_browser_backend(exec_context)
    return await _snapshot_result(exec_context, backend, query=query)


async def browser_click_tool(
    exec_context: ToolExecutionContext, ref: str
) -> ToolResult:
    """Click an element identified by a semantic ref from the latest snapshot."""
    backend = await get_browser_backend(exec_context)
    selector = _resolve_ref(backend, ref)
    logger.info("browser_click: %s -> %s", ref, selector)
    await backend.click(selector)
    await backend.settle()
    return await _snapshot_result(exec_context, backend)


async def browser_fill_tool(
    exec_context: ToolExecutionContext,
    ref: str,
    text: str,
    submit: bool = False,
) -> ToolResult:
    """Fill a text input identified by ``ref``. Optionally press Enter."""
    backend = await get_browser_backend(exec_context)
    selector = _resolve_ref(backend, ref)
    logger.info("browser_fill: %s <- %r (submit=%s)", ref, text, submit)
    await backend.fill(selector, text, submit)
    if submit:
        await backend.settle()
    return await _snapshot_result(exec_context, backend)


async def browser_select_tool(
    exec_context: ToolExecutionContext, ref: str, value: str
) -> ToolResult:
    """Select an ``<option>`` by visible label or value.

    Passing ``value`` positionally lets Playwright match against the option's
    value *or* its visible label in a single call, avoiding a 30s default
    timeout when the LLM guesses value-vs-label wrong.
    """
    backend = await get_browser_backend(exec_context)
    selector = _resolve_ref(backend, ref)
    logger.info("browser_select: %s <- %r", ref, value)
    await backend.select(selector, value)
    return await _snapshot_result(exec_context, backend)


async def browser_wait_tool(
    exec_context: ToolExecutionContext,
    selector: str | None = None,
    state: str = "domcontentloaded",
    timeout_ms: int = 5000,
) -> ToolResult:
    """Wait for a load state or a CSS selector to appear."""
    backend = await get_browser_backend(exec_context)
    logger.info(
        "browser_wait: selector=%s state=%s timeout=%s", selector, state, timeout_ms
    )
    await backend.wait(selector, state, timeout_ms)
    return await _snapshot_result(exec_context, backend)


async def browser_extract_tool(
    exec_context: ToolExecutionContext, selector: str | None = None
) -> ToolResult:
    """Return the page (or a subtree) rendered as Markdown."""
    backend = await get_browser_backend(exec_context)
    html = await backend.extract_html(selector)
    url = backend.current_url
    logger.info(
        "browser_extract: url=%s selector=%s bytes=%s", url, selector, len(html)
    )
    markdown = await convert_html_bytes_to_markdown(
        html.encode("utf-8"), filename=(url or "page") + ".html"
    )
    if markdown is None:
        # ast-grep-ignore: toolresult-text-literal-with-data - error string conveys the same failure mode as the data payload
        return ToolResult(
            text="Failed to convert page to markdown",
            data={"error": "markdown_conversion_failed", "url": url},
        )
    return ToolResult(
        text=markdown,
        data={"url": url, "markdown": markdown, "selector": selector},
    )


async def browser_screenshot_tool(
    exec_context: ToolExecutionContext,
) -> ToolResult:
    """Capture a PNG screenshot of the current page."""
    backend = await get_browser_backend(exec_context)
    png = await backend.screenshot_png()
    url = backend.current_url
    return ToolResult(
        data={"url": url, "bytes": len(png)},
        attachments=[
            ToolAttachment(
                content=png,
                mime_type="image/png",
                description=f"Screenshot of {url}",
            )
        ],
    )


async def browser_exec_tool(
    exec_context: ToolExecutionContext, code: str
) -> ToolResult:
    """Execute arbitrary JavaScript in the current page via ``page.evaluate``.

    The ``code`` is a JS expression or statement list that returns a
    JSON-serializable value. Use this when the fixed tools don't fit — e.g.
    shadow DOM traversal, reading JSON from a same-origin endpoint, or
    multi-step DOM mutation in a single turn.

    The script runs in the page's V8 context with only same-origin privileges.
    It has no access to the Python process or browser internals.
    """
    backend = await get_browser_backend(exec_context)
    logger.info("browser_exec: %d chars", len(code))
    # Clear the ref cache unconditionally — arbitrary JS could have mutated
    # the DOM before throwing (e.g. ``node.remove(); throw new Error()``),
    # so previously-captured refs are unreliable whether or not ``evaluate``
    # succeeds. Doing this up-front keeps the invariant simple: any call to
    # browser_exec means the next click/fill must re-snapshot.
    backend.clear_refs()
    try:
        raw_result = await backend.evaluate(code)
    except BrowserBackendError as exc:
        return ToolResult(
            text=f"JS error: {exc}",
            data={"error": str(exc), "url": backend.current_url},
        )

    await backend.settle(timeout_ms=2000)

    # Surface both the result and the (possibly-changed) URL so the LLM can
    # decide whether to re-snapshot. ``raw_result`` is whatever the JS
    # returned — it's genuinely arbitrary JSON, so it's typed as ``object``
    # rather than a specific shape.
    data: dict[str, object] = {"result": raw_result, "url": backend.current_url}
    return ToolResult(data=data)


async def browser_request_handoff_tool(
    exec_context: ToolExecutionContext,
    reason: str,
    handoff_note: str = "",
    expected_origin: str | None = None,
    allow_resume: bool = False,
) -> ToolResult:
    """Hand the live browser session to a human via the browser-server.

    Use this when a step needs a human: entering payment details, credentials,
    one-time passcodes, accepting legal consent, or solving a CAPTCHA. The
    service mints a one-time URL the human opens to take over the *same* browser
    (via noVNC); the agent loses all observation/control until the human is done.
    Only available when the optional browser-server integration is configured.
    """
    backend = await get_browser_backend(exec_context)
    logger.info("browser_request_handoff: reason=%s", reason)
    try:
        result = await backend.request_handoff(
            reason=reason,
            handoff_note=handoff_note,
            expected_origin=expected_origin,
            allow_resume=allow_resume,
        )
    except HandoffUnavailableError as exc:
        return ToolResult(
            text=f"Browser handoff is not available: {exc}",
            data={"error": "handoff_unavailable", "detail": str(exc)},
        )
    except BrowserBackendError as exc:
        return ToolResult(
            text=f"Browser handoff failed: {exc}",
            data={"error": "handoff_failed", "detail": str(exc)},
        )
    handoff_url = result.get("handoff_url")
    return ToolResult(
        text=(
            f"Handoff requested ({reason}). Ask the user to open this link to take "
            f"over the browser: {handoff_url}"
        ),
        data=result,
    )


async def browser_claim_handback_tool(
    exec_context: ToolExecutionContext,
    session_id: str,
    handback_token: str,
) -> ToolResult:
    """Reclaim a browser session that a human handed back to the agent.

    After the human finishes their task and clicks 'Hand over to agent' in
    the browser UI, they receive a one-time handback token. Pass that token
    and the session ID here to resume full agent control of the same browser
    tab — same URL, cookies, and form state. Only works when the browser-server
    integration is configured and the handoff was requested with allow_resume=true.
    """
    backend = await get_browser_backend(exec_context)
    logger.info("browser_claim_handback: session_id=%s", session_id)
    try:
        await backend.claim_handback(session_id, handback_token)
    except HandoffUnavailableError as exc:
        return ToolResult(
            text=f"Browser handback claim is not available: {exc}",
            data={"error": "handoff_unavailable", "detail": str(exc)},
        )
    except BrowserBackendError as exc:
        return ToolResult(
            text=f"Browser handback claim failed: {exc}",
            data={"error": "claim_failed", "detail": str(exc)},
        )
    # Return a fresh snapshot so the agent sees the current page state
    try:
        snap = await _take_snapshot(backend, query=None)
        return ToolResult(
            text=f"Session reclaimed. Now at: {backend.current_url}",
            data={"claimed": True, "session_id": session_id, "snapshot": snap},
        )
    except BrowserBackendError as exc:
        return ToolResult(
            text=f"Session reclaimed but snapshot failed: {exc}",
            data={"claimed": True, "session_id": session_id},
        )


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


BROWSER_DOM_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "browser_open",
            "description": (
                "Navigate to a URL and return an accessibility snapshot. Combines "
                "navigation + snapshot in one call. Optional `query` filters the "
                "snapshot to elements matching the given substring."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to open."},
                    "query": {
                        "type": "string",
                        "description": "Optional case-insensitive substring filter.",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_snapshot",
            "description": (
                "Return an accessibility snapshot of the current page as indented "
                "TOON text. Use after navigation or DOM mutation to refresh refs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional case-insensitive substring filter.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": (
                "Click an element by its semantic ref (e.g. 'e12') from the last "
                "snapshot. Returns a fresh snapshot after the click."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "string",
                        "description": "Ref id from the last snapshot (e.g. 'e12').",
                    },
                },
                "required": ["ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_fill",
            "description": (
                "Fill an input element identified by ref. If `submit` is true, "
                "presses Enter after filling."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Ref id of the input."},
                    "text": {"type": "string", "description": "Text to fill."},
                    "submit": {
                        "type": "boolean",
                        "description": "Press Enter after filling.",
                        "default": False,
                    },
                },
                "required": ["ref", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_select",
            "description": (
                "Select an <option> on a <select> element by its visible label "
                "(or value as fallback)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Ref id of the <select>."},
                    "value": {
                        "type": "string",
                        "description": "Option label or value.",
                    },
                },
                "required": ["ref", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_wait",
            "description": (
                "Wait for a load state or a CSS selector to appear. Defaults to "
                "waiting for DOMContentLoaded."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "CSS selector to wait for.",
                    },
                    "state": {
                        "type": "string",
                        "description": "Load state: load, domcontentloaded, networkidle.",
                        "default": "domcontentloaded",
                    },
                    "timeout_ms": {
                        "type": "integer",
                        "description": "Timeout in milliseconds.",
                        "default": 5000,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_extract",
            "description": (
                "Return the current page (or a subtree by CSS selector) as "
                "Markdown. Use this when you want page text content rather than "
                "the element tree."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "Optional CSS selector to scope extraction.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": (
                "Take a PNG screenshot of the current page. Use sparingly — the "
                "DOM tools are cheaper. Good for visually verifying state or "
                "sharing with the user."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_exec",
            "description": (
                "Escape hatch: run JavaScript in the current page via "
                "page.evaluate. Pass an expression like "
                '"document.title" or a statement block like '
                "\"{ const h = document.querySelectorAll('h2'); return [...h].map(e => e.innerText); }\". "
                "Returns the script's return value. Use when the fixed tools "
                "don't fit: shadow DOM traversal, iframes, reading JSON from "
                "same-origin endpoints, or custom DOM mutation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "JavaScript expression or statement block.",
                    },
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_request_handoff",
            "description": (
                "Hand the live browser session to a human to finish a step the "
                "agent must not do itself: entering payment details, credentials, "
                "a one-time passcode, accepting legal consent, or solving a CAPTCHA. "
                "Returns a one-time link the user opens to take over the same "
                "browser; the agent loses all access until the human is done. Only "
                "works when the browser-server integration is configured."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "enum": [
                            "payment",
                            "credentials",
                            "otp",
                            "legal_consent",
                            "captcha",
                            "cookie_consent",
                            "other",
                        ],
                        "description": "Why the human needs to take over.",
                    },
                    "handoff_note": {
                        "type": "string",
                        "description": "Short instruction shown to the user on the handoff page.",
                    },
                    "expected_origin": {
                        "type": "string",
                        "description": "Optional origin (scheme+host) the browser must be on before handing off.",
                    },
                    "allow_resume": {
                        "type": "boolean",
                        "description": "If true, the human can hand the browser back to the agent after completing their task. The agent will then call browser_claim_handback with the session_id and handback_token the human receives.",
                    },
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_claim_handback",
            "description": (
                "Reclaim a browser session after a human has handed it back. "
                "Call this with the session_id (from browser_request_handoff result) "
                "and the handback_token the human received after clicking 'Hand over to agent'. "
                "Returns a snapshot of the current page so the agent can continue from "
                "where the human left off. Only works when the browser-server integration "
                "is configured and the original handoff used allow_resume=true."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "The browser session ID from the browser_request_handoff result.",
                    },
                    "handback_token": {
                        "type": "string",
                        "description": "The one-time token the human received after handing back control.",
                    },
                },
                "required": ["session_id", "handback_token"],
            },
        },
    },
]
