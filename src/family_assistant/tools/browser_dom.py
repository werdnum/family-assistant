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

import contextlib
import logging
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict, cast, get_args

from rebrowser_playwright.async_api import Error as PlaywrightError

from family_assistant.tools.browser_session import (
    BrowserSession,
    get_browser_session,
)
from family_assistant.tools.types import ToolAttachment, ToolDefinition, ToolResult
from family_assistant.utils.scraping import convert_html_bytes_to_markdown

if TYPE_CHECKING:
    from rebrowser_playwright.async_api import Page

    from family_assistant.tools.types import ToolExecutionContext

LoadState = Literal["load", "domcontentloaded", "networkidle"]
_VALID_LOAD_STATES: tuple[LoadState, ...] = get_args(LoadState)


def _coerce_load_state(state: str) -> LoadState:
    """Validate and narrow a runtime string to a Playwright load state literal."""
    if state not in _VALID_LOAD_STATES:
        raise ValueError(
            f"Invalid load state {state!r}; expected one of {_VALID_LOAD_STATES}"
        )
    for candidate in _VALID_LOAD_STATES:
        if candidate == state:
            return candidate
    raise AssertionError("unreachable")


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


logger = logging.getLogger(__name__)

__all__ = [
    "BROWSER_DOM_TOOLS_DEFINITION",
    "browser_click_tool",
    "browser_exec_tool",
    "browser_extract_tool",
    "browser_fill_tool",
    "browser_open_tool",
    "browser_screenshot_tool",
    "browser_select_tool",
    "browser_snapshot_tool",
    "browser_wait_tool",
]


# ---------------------------------------------------------------------------
# Snapshot building
# ---------------------------------------------------------------------------

# JS that walks the DOM, tags interactive/labeled elements with a stable
# ``data-ref`` attribute, and returns a nested structure describing each
# element's role, accessible name, and key attributes. The attribute-tagging
# strategy means the Python side doesn't have to store a per-ref selector —
# the ref ``e12`` always resolves to ``[data-ref="e12"]``.
#
# The function is wrapped in an IIFE so it can be passed straight to
# ``page.evaluate`` without leaking globals.
_SNAPSHOT_JS = r"""
() => {
  // Clear previous refs so snapshots between navigations don't collide.
  document.querySelectorAll('[data-ref]').forEach(el => el.removeAttribute('data-ref'));

  let refCounter = 0;
  const allocRef = () => 'e' + (++refCounter);

  const ROLE_MAP = {
    A: 'link', BUTTON: 'button', SELECT: 'combobox',
    TEXTAREA: 'textbox', FORM: 'form', NAV: 'navigation',
    MAIN: 'main', ASIDE: 'complementary', HEADER: 'banner',
    FOOTER: 'contentinfo', IMG: 'img',
  };
  const INPUT_ROLES = {
    submit: 'button', button: 'button', reset: 'button',
    checkbox: 'checkbox', radio: 'radio',
    range: 'slider', file: 'textbox',
  };
  const HEADING_TAGS = new Set(['H1','H2','H3','H4','H5','H6']);

  function roleFor(el) {
    const aria = el.getAttribute('role');
    if (aria) return aria;
    if (HEADING_TAGS.has(el.tagName)) return 'heading';
    if (el.tagName === 'INPUT') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      return INPUT_ROLES[t] || 'textbox';
    }
    return ROLE_MAP[el.tagName] || null;
  }

  function accName(el) {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const target = document.getElementById(labelledBy);
      if (target) return target.textContent.trim();
    }
    if (el.getAttribute('alt')) return el.getAttribute('alt').trim();
    if (el.getAttribute('title')) return el.getAttribute('title').trim();
    if (el.getAttribute('placeholder')) return el.getAttribute('placeholder').trim();
    if (el.id) {
      const lbl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lbl) return lbl.textContent.trim();
    }
    const parentLabel = el.closest && el.closest('label');
    if (parentLabel && parentLabel !== el) return parentLabel.textContent.trim();
    const txt = (el.innerText || el.textContent || '').trim();
    return txt.length > 120 ? txt.slice(0, 120) + '…' : txt;
  }

  function isVisible(el) {
    if (!el.getBoundingClientRect) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return false;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    return true;
  }

  function interesting(el) {
    const role = roleFor(el);
    if (role) return role;
    if (el.tagName === 'P' || el.tagName === 'LI') return 'text';
    return null;
  }

  function walk(el, out) {
    if (el.nodeType !== 1) return;
    if (!isVisible(el)) return;
    const role = interesting(el);
    if (role) {
      const ref = allocRef();
      el.setAttribute('data-ref', ref);
      const node = { ref, role, name: accName(el) };
      const href = el.getAttribute('href');
      if (href) node.href = href;
      const value = el.value;
      if (typeof value === 'string' && value) node.value = value;
      if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') {
        node.tag = el.tagName.toLowerCase();
        const t = el.getAttribute('type');
        if (t) node.input_type = t.toLowerCase();
      }
      out.push(node);
      node.children = [];
      for (const child of el.children) walk(child, node.children);
      if (node.children.length === 0) delete node.children;
    } else {
      for (const child of el.children) walk(child, out);
    }
  }

  const roots = [];
  walk(document.body, roots);

  const formCount = document.forms ? document.forms.length : 0;
  return {
    url: location.href,
    title: document.title,
    forms: formCount,
    elements: refCounter,
    roots,
  };
}
"""


def _format_toon(snapshot: Snapshot, query: str | None = None) -> str:
    """Render an accessibility snapshot as indented TOON-style text."""
    lines: list[str] = [
        "page:",
        f"  url: {snapshot['url']}",
        f"  title: {snapshot['title']}",
        f"  forms: {snapshot['forms']}",
        f"  elements: {snapshot['elements']}",
        "",
    ]

    def match(node: SnapshotNode) -> bool:
        if not query:
            return True
        needle = query.lower()
        return (
            needle in node["name"].lower()
            or needle in node["role"].lower()
            or needle in node.get("href", "").lower()
        )

    def render(nodes: list[SnapshotNode], depth: int, include_all: bool) -> None:
        indent = "  " * depth
        for node in nodes:
            children = node.get("children", [])
            # When filtering, keep a node if it matches OR any descendant matches.
            if not include_all and not (match(node) or _any_match(children, query)):
                continue
            attrs: list[str] = []
            if "href" in node:
                attrs.append(f"href={node['href']}")
            if "input_type" in node:
                attrs.append(f"type={node['input_type']}")
            if "value" in node:
                attrs.append(f"value={node['value']!r}")
            suffix = f" {' '.join(attrs)}" if attrs else ""
            label = f' "{node["name"]}"' if node["name"] else ""
            lines.append(f"{indent}[{node['ref']}] {node['role']}{label}{suffix}")
            if children:
                render(children, depth + 1, include_all)

    render(snapshot["roots"], depth=1, include_all=query is None)
    if query and len(lines) == 6:
        lines.append(f"  (no matches for query={query!r})")
    return "\n".join(lines).rstrip() + "\n"


def _any_match(nodes: list[SnapshotNode], query: str | None) -> bool:
    if not query:
        return True
    needle = query.lower()
    for node in nodes:
        if (
            needle in node["name"].lower()
            or needle in node["role"].lower()
            or needle in node.get("href", "").lower()
        ):
            return True
        if _any_match(node.get("children", []), query):
            return True
    return False


def _collect_refs(
    nodes: list[SnapshotNode], out: dict[str, str] | None = None
) -> dict[str, str]:
    """Walk a snapshot tree, returning ``{ref: selector}`` for every node."""
    if out is None:
        out = {}
    for node in nodes:
        out[node["ref"]] = f'[data-ref="{node["ref"]}"]'
        children = node.get("children")
        if children:
            _collect_refs(children, out)
    return out


async def _take_snapshot(
    session: BrowserSession, page: Page, query: str | None
) -> SnapshotData:
    """Run the snapshot JS, update the session ref cache, and return the snapshot."""
    raw = await page.evaluate(_SNAPSHOT_JS)
    # page.evaluate returns the raw JSON the JS produced; the shape is
    # controlled entirely by ``_SNAPSHOT_JS`` above, which matches Snapshot.
    snapshot = cast("Snapshot", raw)
    session.ref_cache.clear()
    session.ref_cache.update(_collect_refs(snapshot["roots"]))
    text = _format_toon(snapshot, query=query)
    return SnapshotData(
        text=text,
        url=snapshot["url"] or page.url,
        title=snapshot["title"],
        counts=SnapshotCounts(forms=snapshot["forms"], elements=snapshot["elements"]),
        refs=list(session.ref_cache.keys()),
    )


def _resolve_ref(session: BrowserSession, ref: str) -> str:
    """Return the selector for ``ref`` or raise a clear error."""
    selector = session.ref_cache.get(ref)
    if selector is None:
        raise ValueError(
            f"Unknown ref {ref!r}. Refs are only valid for the most recent "
            f"snapshot; call browser_snapshot again after navigation or DOM "
            f"changes. Known refs: {sorted(session.ref_cache.keys())[:10]}…"
        )
    return selector


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def browser_open_tool(
    exec_context: ToolExecutionContext, url: str, query: str | None = None
) -> ToolResult:
    """Navigate to a URL and return a snapshot in one call."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    session = await get_browser_session(exec_context)
    page = await session.ensure_page()
    logger.info("browser_open: %s", url)
    await page.goto(url)
    with contextlib.suppress(PlaywrightError):
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
    snap = await _take_snapshot(session, page, query=query)
    return ToolResult(text=snap["text"], data=dict(snap))


async def browser_snapshot_tool(
    exec_context: ToolExecutionContext, query: str | None = None
) -> ToolResult:
    """Re-capture an accessibility snapshot of the current page."""
    session = await get_browser_session(exec_context)
    page = await session.ensure_page()
    snap = await _take_snapshot(session, page, query=query)
    return ToolResult(text=snap["text"], data=dict(snap))


async def browser_click_tool(
    exec_context: ToolExecutionContext, ref: str
) -> ToolResult:
    """Click an element identified by a semantic ref from the latest snapshot."""
    session = await get_browser_session(exec_context)
    page = await session.ensure_page()
    selector = _resolve_ref(session, ref)
    logger.info("browser_click: %s -> %s", ref, selector)
    await page.locator(selector).click()
    with contextlib.suppress(PlaywrightError):
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
    snap = await _take_snapshot(session, page, query=None)
    return ToolResult(text=snap["text"], data=dict(snap))


async def browser_fill_tool(
    exec_context: ToolExecutionContext,
    ref: str,
    text: str,
    submit: bool = False,
) -> ToolResult:
    """Fill a text input identified by ``ref``. Optionally press Enter."""
    session = await get_browser_session(exec_context)
    page = await session.ensure_page()
    selector = _resolve_ref(session, ref)
    logger.info("browser_fill: %s <- %r (submit=%s)", ref, text, submit)
    locator = page.locator(selector)
    await locator.fill(text)
    if submit:
        await locator.press("Enter")
        with contextlib.suppress(PlaywrightError):
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
    snap = await _take_snapshot(session, page, query=None)
    return ToolResult(text=snap["text"], data=dict(snap))


async def browser_select_tool(
    exec_context: ToolExecutionContext, ref: str, value: str
) -> ToolResult:
    """Select an ``<option>`` by visible label or value."""
    session = await get_browser_session(exec_context)
    page = await session.ensure_page()
    selector = _resolve_ref(session, ref)
    logger.info("browser_select: %s <- %r", ref, value)
    try:
        await page.locator(selector).select_option(label=value)
    except PlaywrightError:
        await page.locator(selector).select_option(value=value)
    snap = await _take_snapshot(session, page, query=None)
    return ToolResult(text=snap["text"], data=dict(snap))


async def browser_wait_tool(
    exec_context: ToolExecutionContext,
    selector: str | None = None,
    state: str = "domcontentloaded",
    timeout_ms: int = 5000,
) -> ToolResult:
    """Wait for a load state or a CSS selector to appear."""
    session = await get_browser_session(exec_context)
    page = await session.ensure_page()
    if selector:
        logger.info("browser_wait: selector=%s timeout=%s", selector, timeout_ms)
        await page.wait_for_selector(selector, timeout=timeout_ms)
    else:
        load_state = _coerce_load_state(state)
        logger.info("browser_wait: state=%s timeout=%s", load_state, timeout_ms)
        await page.wait_for_load_state(load_state, timeout=timeout_ms)
    snap = await _take_snapshot(session, page, query=None)
    return ToolResult(text=snap["text"], data=dict(snap))


async def browser_extract_tool(
    exec_context: ToolExecutionContext, selector: str | None = None
) -> ToolResult:
    """Return the page (or a subtree) rendered as Markdown."""
    session = await get_browser_session(exec_context)
    page = await session.ensure_page()
    if selector:
        html = await page.locator(selector).inner_html()
    else:
        html = await page.content()
    logger.info(
        "browser_extract: url=%s selector=%s bytes=%s", page.url, selector, len(html)
    )
    markdown = await convert_html_bytes_to_markdown(
        html.encode("utf-8"), filename=(page.url or "page") + ".html"
    )
    if markdown is None:
        # ast-grep-ignore: toolresult-text-literal-with-data - error string conveys the same failure mode as the data payload
        return ToolResult(
            text="Failed to convert page to markdown",
            data={"error": "markdown_conversion_failed", "url": page.url},
        )
    return ToolResult(
        text=markdown,
        data={"url": page.url, "markdown": markdown, "selector": selector},
    )


async def browser_screenshot_tool(
    exec_context: ToolExecutionContext,
) -> ToolResult:
    """Capture a PNG screenshot of the current page."""
    session = await get_browser_session(exec_context)
    page = await session.ensure_page()
    png = await page.screenshot(type="png")
    return ToolResult(
        data={"url": page.url, "bytes": len(png)},
        attachments=[
            ToolAttachment(
                content=png,
                mime_type="image/png",
                description=f"Screenshot of {page.url}",
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
    session = await get_browser_session(exec_context)
    page = await session.ensure_page()
    logger.info("browser_exec: %d chars", len(code))
    try:
        # Accept both expressions ("document.title") and statement blocks
        # ("{ const x = 1; return x * 2; }") by wrapping in an arrow function
        # when the code isn't already one.
        wrapped = code.strip()
        if not wrapped.startswith(("(", "async ", "()")):
            if wrapped.startswith("{"):
                wrapped = f"() => {wrapped}"
            else:
                wrapped = f"() => ({wrapped})"
        raw_result = await page.evaluate(wrapped)
    except PlaywrightError as exc:
        return ToolResult(
            text=f"JS error: {exc}",
            data={"error": str(exc), "url": page.url},
        )

    with contextlib.suppress(PlaywrightError):
        await page.wait_for_load_state("domcontentloaded", timeout=2000)

    # Clear the ref cache — arbitrary JS could have mutated the DOM, so any
    # previously-captured refs are now unreliable.
    session.clear_refs()

    # Surface both the result and the (possibly-changed) URL so the LLM can
    # decide whether to re-snapshot. ``raw_result`` is whatever the JS
    # returned — it's genuinely arbitrary JSON, so it's typed as ``object``
    # rather than a specific shape.
    data: dict[str, object] = {"result": raw_result, "url": page.url}
    return ToolResult(data=data)


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
]
