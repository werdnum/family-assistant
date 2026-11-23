"""Functional tests for the semantic DOM browser tools.

These tests exercise the full Playwright-driven stack: they spin up a tiny
aiohttp server serving deterministic HTML fixtures, drive a real headless
``rebrowser_playwright`` browser against it, and verify the end-to-end tool
flow — snapshot → click/fill → extract → exec.

The tests are deliberately self-contained: no third-party network access, no
VCR cassettes, no Playwright browser context fixtures beyond what the tool
itself manages. This keeps them fast and reliable while still proving the
tools work against a real browser.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from rebrowser_playwright.async_api import async_playwright

from family_assistant.tools.browser_dom import (
    SnapshotNode,
    browser_click_at_tool,
    browser_click_tool,
    browser_exec_tool,
    browser_extract_tool,
    browser_fill_tool,
    browser_hover_at_tool,
    browser_hover_tool,
    browser_open_tool,
    browser_press_key_tool,
    browser_screenshot_tool,
    browser_scroll_tool,
    browser_select_tool,
    browser_snapshot_tool,
    browser_wait_tool,
)
from family_assistant.tools.browser_session import (
    close_browser_session,
    get_browser_session,
)
from family_assistant.tools.computer_use import computer_use_navigate
from family_assistant.tools.types import (
    ToolCallBatch,
    ToolExecutionContext,
    ToolResult,
)
from family_assistant.utils.stealth_browser import launch_stealth_browser
from tests.mocks.unconnected_database import unconnected_database

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


pytestmark = pytest.mark.asyncio


INDEX_HTML = """<!doctype html>
<html>
  <head><title>Index</title></head>
  <body>
    <h1>Welcome to the test site</h1>
    <p id="intro">This is the intro paragraph.</p>
    <form action="/submit" method="get">
      <label for="q">Search query</label>
      <input id="q" name="q" type="text" placeholder="Type here" />
      <label for="color">Color</label>
      <select id="color" name="color">
        <option value="red">Red</option>
        <option value="green">Green</option>
        <option value="blue">Blue</option>
      </select>
      <button type="submit">Go</button>
    </form>
    <a id="about-link" href="/about">About this site</a>
  </body>
</html>
"""

ABOUT_HTML = """<!doctype html>
<html>
  <head><title>About</title></head>
  <body>
    <h1>About</h1>
    <p>This is the about page.</p>
  </body>
</html>
"""

SUBMIT_HTML_TEMPLATE = """<!doctype html>
<html>
  <head><title>Results</title></head>
  <body>
    <h1>Results</h1>
    <p id="echo">You searched for: {query}</p>
    <p id="color">Color: {color}</p>
  </body>
</html>
"""

# Exercises the multi-ID form of ``aria-labelledby``: the input below should
# pick up "Quantity lbs" as its accessible name, not just the first referenced
# span.
LABELLEDBY_HTML = """<!doctype html>
<html>
  <head><title>Labelled</title></head>
  <body>
    <span id="lbl-a">Quantity</span>
    <span id="lbl-b">lbs</span>
    <input id="qty" type="number" aria-labelledby="lbl-a lbl-b" />
  </body>
</html>
"""


# Two independently addressable inputs, for batched ref actions.
TWO_INPUTS_HTML = """<!doctype html>
<html>
  <head><title>Two inputs</title></head>
  <body>
    <h1>Two inputs</h1>
    <label for="first">First</label>
    <input id="first" name="first" type="text" />
    <label for="second">Second</label>
    <input id="second" name="second" type="text" />
  </body>
</html>
"""


async def _two_inputs(_request: web.Request) -> web.Response:
    return web.Response(text=TWO_INPUTS_HTML, content_type="text/html")


INTERACTIVE_HTML = """<!doctype html>
<html>
  <head>
    <title>Interactive</title>
    <style>
      html, body { margin: 0; padding: 0; height: 2400px; }
      #arrow-icon {
        position: absolute;
        left: 300px;
        top: 120px;
        width: 40px;
        height: 40px;
        background: #2563eb;
      }
      #lower-icon {
        position: absolute;
        left: 200px;
        top: 1200px;
        width: 60px;
        height: 40px;
        background: #16a34a;
      }
      #scroll-box {
        position: absolute;
        left: 600px;
        top: 100px;
        width: 240px;
        height: 160px;
        overflow-y: scroll;
        border: 1px solid #333;
      }
      #scroll-inner {
        height: 900px;
      }
      #status {
        position: fixed;
        top: 8px;
        left: 8px;
      }
    </style>
  </head>
  <body>
    <p id="status">idle</p>
    <div id="arrow-icon"></div>
    <div id="lower-icon"></div>
    <div id="scroll-box">
      <div id="scroll-inner">
        <p id="box-scroll">box:0</p>
      </div>
    </div>
    <script>
      const status = document.getElementById('status');
      const arrow = document.getElementById('arrow-icon');
      arrow.addEventListener('mouseenter', () => { status.textContent = 'hovered-arrow'; });
      arrow.addEventListener('click', () => { status.textContent = 'clicked-arrow'; });
      const lower = document.getElementById('lower-icon');
      lower.addEventListener('click', () => { status.textContent = 'clicked-lower'; });
      const box = document.getElementById('scroll-box');
      box.addEventListener('scroll', () => {
        document.getElementById('box-scroll').textContent = 'box:' + Math.round(box.scrollTop);
      });
    </script>
  </body>
</html>
"""

OVERLAYS_HTML = """<!doctype html>
<html>
  <head>
    <title>Overlays</title>
    <style>
      body { margin: 40px; }
      #card-wrap { position: relative; display: inline-block; }
      #hover-preview {
        display: none;
        position: fixed;
        top: 0;
        left: 220px;
        right: 0;
        bottom: 0;
        background: rgba(0,0,0,0.4);
        z-index: 10;
      }
      #modal {
        display: block;
        position: fixed;
        top: 20px;
        right: 20px;
        padding: 12px;
        background: white;
        border: 1px solid #333;
        z-index: 5;
      }
    </style>
  </head>
  <body>
    <h1>Overlay test</h1>
    <p id="state">initial</p>
    <div id="card-wrap">
      <button id="card-btn" type="button">Hover card</button>
      <div id="hover-preview"><p>Preview overlay active</p></div>
    </div>
    <button id="download-btn" type="button" style="position: fixed; left: 260px; top: 80px;">Download 3MF</button>
    <div id="modal"><p>Modal open</p></div>
    <script>
      const state = document.getElementById('state');
      const cardBtn = document.getElementById('card-btn');
      const preview = document.getElementById('hover-preview');
      const modal = document.getElementById('modal');
      cardBtn.addEventListener('mouseenter', () => {
        preview.style.display = 'block';
        state.textContent = 'overlay-shown';
      });
      preview.addEventListener('mouseleave', () => {
        preview.style.display = 'none';
        state.textContent = 'overlay-hidden';
      });
      document.addEventListener('mousemove', (ev) => {
        if (ev.clientX <= 5 && ev.clientY <= 5 && preview.style.display === 'block') {
          preview.style.display = 'none';
          state.textContent = 'overlay-dismissed';
        }
      });
      document.getElementById('download-btn').addEventListener('click', () => {
        state.textContent = 'downloaded';
      });
      document.addEventListener('keydown', (ev) => {
        if (ev.key === 'Escape') {
          modal.style.display = 'none';
          state.textContent = 'modal-closed';
        }
      });
    </script>
  </body>
</html>
"""


async def _interactive(_request: web.Request) -> web.Response:
    return web.Response(text=INTERACTIVE_HTML, content_type="text/html")


async def _overlays(_request: web.Request) -> web.Response:
    return web.Response(text=OVERLAYS_HTML, content_type="text/html")


async def _index(_request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type="text/html")


async def _about(_request: web.Request) -> web.Response:
    return web.Response(text=ABOUT_HTML, content_type="text/html")


async def _submit(request: web.Request) -> web.Response:
    query = request.query.get("q", "")
    color = request.query.get("color", "")
    return web.Response(
        text=SUBMIT_HTML_TEMPLATE.format(query=query, color=color),
        content_type="text/html",
    )


async def _labelledby(_request: web.Request) -> web.Response:
    return web.Response(text=LABELLEDBY_HTML, content_type="text/html")


@dataclass
class BoundFixtureServer:
    """Minimal handle onto a running aiohttp test server.

    ``aiohttp.test_utils.TestServer`` handles the transport plumbing; this
    wrapper just exposes the bound ``url`` so individual tests don't need to
    stitch host/port together themselves.
    """

    server: TestServer
    url: str


@pytest.fixture
async def fixture_server() -> AsyncGenerator[BoundFixtureServer]:
    """Spin up a local aiohttp server serving the test fixtures."""
    app = web.Application()
    app.router.add_get("/", _index)
    app.router.add_get("/about", _about)
    app.router.add_get("/submit", _submit)
    app.router.add_get("/labelledby", _labelledby)
    app.router.add_get("/two-inputs", _two_inputs)
    app.router.add_get("/interactive", _interactive)
    app.router.add_get("/overlays", _overlays)

    server = TestServer(app)
    await server.start_server()
    try:
        yield BoundFixtureServer(
            server=server, url=str(server.make_url("/")).rstrip("/")
        )
    finally:
        await server.close()


@pytest.fixture
def exec_context(request: pytest.FixtureRequest) -> ToolExecutionContext:
    """A minimal top-level ToolExecutionContext with a unique conversation id.

    A top-level turn (no subconversation) keys its ``BrowserSession`` by
    ``conversation_id``. Using the test's nodeid keeps each test's session
    isolated. The browser tools never query the database on a top-level turn.
    """
    return ToolExecutionContext(
        interface_type="test",
        conversation_id=f"browser-dom-test-{request.node.nodeid}",
        user_name="test_user",
        turn_id=None,
        db_context=unconnected_database(),
        processing_service=None,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        credential_resolvers=None,
        api_backend=None,
        timezone=ZoneInfo("UTC"),
    )


_CallFactory = Callable[[ToolExecutionContext], Awaitable[ToolResult]]


async def _run_batch(
    exec_context: ToolExecutionContext, calls: list[tuple[str, str, _CallFactory]]
) -> list[ToolResult]:
    """Run ``(call_id, tool_name, coroutine factory)`` calls as one model response.

    Mirrors the loop: one batch, tasks created in issue order, each reporting
    completion when it finishes however it finishes.
    """
    batch = ToolCallBatch([(call_id, name) for call_id, name, _ in calls])

    async def _one(call_id: str, make: _CallFactory) -> ToolResult:
        try:
            return await make(
                replace(exec_context, tool_call_batch=batch, tool_call_id=call_id)
            )
        finally:
            batch.mark_done(call_id)

    return await asyncio.gather(*[_one(call_id, make) for call_id, _, make in calls])


@pytest.fixture(autouse=True)
async def _cleanup_browser_session(
    exec_context: ToolExecutionContext,
) -> AsyncGenerator[None]:
    """Ensure the per-test browser session is torn down after each test."""
    yield
    await close_browser_session(exec_context)


async def test_browser_open_returns_snapshot_with_refs(
    fixture_server: BoundFixtureServer, exec_context: ToolExecutionContext
) -> None:
    result = await browser_open_tool(exec_context, fixture_server.url + "/")
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["title"] == "Index"
    assert data["url"].rstrip("/") == fixture_server.url
    # The page has at least one form, one heading, one input, one select,
    # and one link — refs should include all of them.
    assert data["counts"]["forms"] == 1
    refs = data["refs"]
    assert isinstance(refs, list)
    assert len(refs) >= 5
    # TOON text surfaces the link label so the LLM can find it.
    assert "About this site" in result.get_text()


async def test_browser_fill_and_submit_navigates(
    fixture_server: BoundFixtureServer, exec_context: ToolExecutionContext
) -> None:
    await browser_open_tool(exec_context, fixture_server.url + "/")
    snap = await browser_snapshot_tool(exec_context, query="search")
    input_ref = _first_ref_for(snap, role="textbox")

    result = await browser_fill_tool(
        exec_context, ref=input_ref, text="kittens", submit=True
    )
    data = result.get_data()
    assert isinstance(data, dict)
    assert "/submit" in data["url"]
    assert "kittens" in result.get_text() or "kittens" in str(data)


async def test_browser_select_changes_value(
    fixture_server: BoundFixtureServer, exec_context: ToolExecutionContext
) -> None:
    await browser_open_tool(exec_context, fixture_server.url + "/")
    snap = await browser_snapshot_tool(exec_context)
    select_ref = _first_ref_for(snap, role="combobox")

    await browser_select_tool(exec_context, ref=select_ref, value="Green")
    # Verify the DOM state changed by reading .value via browser_exec.
    exec_result = await browser_exec_tool(
        exec_context, code="return document.querySelector('#color').value"
    )
    exec_data = exec_result.get_data()
    assert isinstance(exec_data, dict)
    assert exec_data["result"] == "green"


async def test_browser_click_follows_link(
    fixture_server: BoundFixtureServer, exec_context: ToolExecutionContext
) -> None:
    await browser_open_tool(exec_context, fixture_server.url + "/")
    snap = await browser_snapshot_tool(exec_context, query="about")
    link_ref = _first_ref_for(snap, role="link")

    result = await browser_click_tool(exec_context, ref=link_ref)
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["title"] == "About"
    assert data["url"].endswith("/about")


async def test_browser_extract_returns_markdown(
    fixture_server: BoundFixtureServer, exec_context: ToolExecutionContext
) -> None:
    await browser_open_tool(exec_context, fixture_server.url + "/about")
    result = await browser_extract_tool(exec_context)
    data = result.get_data()
    assert isinstance(data, dict)
    # MarkItDown should surface the heading and the body paragraph as text.
    markdown = data["markdown"]
    assert "About" in markdown
    assert "about page" in markdown


async def test_browser_wait_returns_snapshot(
    fixture_server: BoundFixtureServer, exec_context: ToolExecutionContext
) -> None:
    await browser_open_tool(exec_context, fixture_server.url + "/")
    result = await browser_wait_tool(
        exec_context, selector="#intro", state="domcontentloaded", timeout_ms=3000
    )
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["title"] == "Index"


async def test_browser_screenshot_attaches_png(
    fixture_server: BoundFixtureServer, exec_context: ToolExecutionContext
) -> None:
    await browser_open_tool(exec_context, fixture_server.url + "/")
    result = await browser_screenshot_tool(exec_context)
    assert result.attachments
    attachment = result.attachments[0]
    assert attachment.mime_type == "image/png"
    # PNG magic bytes — cheap sanity check that the screenshot is real.
    assert isinstance(attachment.content, bytes)
    assert attachment.content[:4] == b"\x89PNG"


async def test_browser_exec_runs_js_and_returns_json_value(
    fixture_server: BoundFixtureServer, exec_context: ToolExecutionContext
) -> None:
    await browser_open_tool(exec_context, fixture_server.url + "/")
    result = await browser_exec_tool(
        exec_context,
        code=(
            "return Array.from(document.querySelectorAll('h1')).map(h => h.textContent)"
        ),
    )
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["result"] == ["Welcome to the test site"]


async def test_browser_exec_handles_js_errors_gracefully(
    fixture_server: BoundFixtureServer, exec_context: ToolExecutionContext
) -> None:
    await browser_open_tool(exec_context, fixture_server.url + "/")
    result = await browser_exec_tool(
        exec_context, code="return nonexistent_variable.foo"
    )
    data = result.get_data()
    assert isinstance(data, dict)
    # The tool should capture the JS exception, not propagate it.
    assert "error" in data
    assert "nonexistent_variable" in data["error"]


async def test_click_after_browser_exec_still_works(
    fixture_server: BoundFixtureServer, exec_context: ToolExecutionContext
) -> None:
    """A script that leaves the page alone leaves refs alone too.

    The old ref cache was wiped by every browser_exec, so this click used to
    fail on a page where nothing had changed.
    """
    snap = await browser_open_tool(exec_context, fixture_server.url + "/")
    link_ref = _first_ref_for(snap, role="link")

    await browser_exec_tool(exec_context, code="return document.title")

    result = await browser_click_tool(exec_context, ref=link_ref)
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["title"] == "About"


async def test_click_on_a_removed_node_returns_the_error_with_a_snapshot(
    fixture_server: BoundFixtureServer, exec_context: ToolExecutionContext
) -> None:
    """A miss comes back with the page as it is now, so no extra round trip."""
    snap = await browser_open_tool(exec_context, fixture_server.url + "/")
    link_ref = _first_ref_for(snap, role="link")

    await browser_exec_tool(
        exec_context,
        code="document.querySelector('#about-link').remove(); return true;",
    )

    result = await browser_click_tool(exec_context, ref=link_ref)
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["error"] == "stale_ref"
    assert data["ref"] == link_ref
    assert data["roots"], "the miss must carry a snapshot of the current page"
    assert "no longer on the page as snapshotted" in result.get_text()
    # The page did not navigate, so the rest of it is still addressable.
    assert data["title"] == "Index"


async def test_a_ref_from_the_previous_page_fails_with_the_current_page(
    fixture_server: BoundFixtureServer, exec_context: ToolExecutionContext
) -> None:
    """The failure a ref cache could not prevent: a ref copied across pages."""
    first = await browser_open_tool(exec_context, fixture_server.url + "/")
    old_ref = _first_ref_for(first, role="link")

    await browser_open_tool(exec_context, fixture_server.url + "/about")

    result = await browser_click_tool(exec_context, ref=old_ref)
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["error"] == "stale_ref"
    assert data["title"] == "About"
    assert old_ref not in data["refs"]


async def test_refs_are_unchanged_across_snapshots_and_actions(
    fixture_server: BoundFixtureServer, exec_context: ToolExecutionContext
) -> None:
    first = await browser_open_tool(exec_context, fixture_server.url + "/")
    second = await browser_snapshot_tool(exec_context)
    first_data = first.get_data()
    second_data = second.get_data()
    assert isinstance(first_data, dict)
    assert isinstance(second_data, dict)
    assert second_data["refs"] == first_data["refs"]

    select_ref = _first_ref_for(first, role="combobox")
    after_action = await browser_select_tool(
        exec_context, ref=select_ref, value="Green"
    )
    after_data = after_action.get_data()
    assert isinstance(after_data, dict)
    # Selecting an option does not renumber the untouched nodes.
    assert _first_ref_for(after_action, role="link") == _first_ref_for(
        first, role="link"
    )
    assert select_ref in after_data["refs"]


async def test_two_ref_actions_in_one_batch_each_land_on_their_own_node(
    fixture_server: BoundFixtureServer, exec_context: ToolExecutionContext
) -> None:
    """Concurrent siblings run in issue order, each against the live page."""
    snap = await browser_open_tool(exec_context, fixture_server.url + "/two-inputs")
    first_ref = _ref_for_name(snap, "First")
    second_ref = _ref_for_name(snap, "Second")

    results = await _run_batch(
        exec_context,
        [
            (
                "call_1",
                "browser_fill",
                lambda ctx: browser_fill_tool(ctx, ref=first_ref, text="alpha"),
            ),
            (
                "call_2",
                "browser_fill",
                lambda ctx: browser_fill_tool(ctx, ref=second_ref, text="beta"),
            ),
        ],
    )

    values = await browser_exec_tool(
        exec_context,
        code=(
            "return [document.querySelector('#first').value, "
            "document.querySelector('#second').value]"
        ),
    )
    values_data = values.get_data()
    assert isinstance(values_data, dict)
    assert values_data["result"] == ["alpha", "beta"]

    first_result, second_result = (r.get_data() for r in results)
    assert isinstance(first_result, dict)
    assert isinstance(second_result, dict)
    # Only the last snapshot-bearing call in a batch hands back refs.
    assert first_result["refs"] == []
    assert "refs omitted" in results[0].get_text()
    assert second_ref in second_result["refs"]


async def test_a_batched_action_after_a_navigating_sibling_fails_rather_than_acting(
    fixture_server: BoundFixtureServer, exec_context: ToolExecutionContext
) -> None:
    """Ordering alone is not enough; unique numbering is what makes it safe."""
    snap = await browser_open_tool(exec_context, fixture_server.url + "/")
    link_ref = _first_ref_for(snap, role="link")
    input_ref = _first_ref_for(snap, role="textbox")

    results = await _run_batch(
        exec_context,
        [
            (
                "call_1",
                "browser_click",
                lambda ctx: browser_click_tool(ctx, ref=link_ref),
            ),
            (
                "call_2",
                "browser_fill",
                lambda ctx: browser_fill_tool(ctx, ref=input_ref, text="kittens"),
            ),
        ],
    )

    second = results[1].get_data()
    assert isinstance(second, dict)
    assert second["error"] == "stale_ref"
    # The failure reports the page the click navigated to, not the old one.
    assert second["title"] == "About"


async def test_a_batch_that_navigates_twice_hands_back_refs_only_at_the_end(
    fixture_server: BoundFixtureServer, exec_context: ToolExecutionContext
) -> None:
    results = await _run_batch(
        exec_context,
        [
            (
                "call_1",
                "browser_open",
                lambda ctx: browser_open_tool(ctx, fixture_server.url + "/"),
            ),
            (
                "call_2",
                "browser_open",
                lambda ctx: browser_open_tool(ctx, fixture_server.url + "/about"),
            ),
        ],
    )

    first, second = (r.get_data() for r in results)
    assert isinstance(first, dict)
    assert isinstance(second, dict)
    assert first["title"] == "Index"
    assert first["refs"] == []
    assert "refs omitted" in results[0].get_text()
    assert second["title"] == "About"
    assert second["refs"]


async def test_filtered_post_click_snapshot_keeps_usable_refs(
    fixture_server: BoundFixtureServer, exec_context: ToolExecutionContext
) -> None:
    """A `query` on an action prunes the snapshot without breaking its refs."""
    snap = await browser_open_tool(exec_context, fixture_server.url + "/two-inputs")
    first_ref = _ref_for_name(snap, "First")

    filtered = await browser_fill_tool(
        exec_context, ref=first_ref, text="alpha", query="second"
    )
    # The filter prunes what the model reads; the structured data stays whole.
    rendered = filtered.get_text()
    assert "Second" in rendered
    assert "First" not in rendered

    second_ref = _ref_for_name(filtered, "Second")
    await browser_fill_tool(exec_context, ref=second_ref, text="beta")
    values = await browser_exec_tool(
        exec_context, code="return document.querySelector('#second').value"
    )
    values_data = values.get_data()
    assert isinstance(values_data, dict)
    assert values_data["result"] == "beta"


async def test_invalid_ref_is_rejected_before_the_browser(
    fixture_server: BoundFixtureServer, exec_context: ToolExecutionContext
) -> None:
    await browser_open_tool(exec_context, fixture_server.url + "/")
    with pytest.raises(ValueError, match="Invalid ref"):
        await browser_click_tool(exec_context, ref="not-a-ref")


async def test_computer_use_navigation_leaves_old_page_refs_failing(
    fixture_server: BoundFixtureServer, exec_context: ToolExecutionContext
) -> None:
    """The two browser profiles share one tab, so a ref must not survive it.

    Nothing is invalidated client-side: the ref simply finds no node on the
    page the visual profile navigated to.
    """
    snap = await browser_open_tool(exec_context, fixture_server.url + "/")
    link_ref = _first_ref_for(snap, role="link")

    await computer_use_navigate(exec_context, fixture_server.url + "/about")

    result = await browser_click_tool(exec_context, ref=link_ref)
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["error"] == "stale_ref"
    assert data["title"] == "About"


async def test_aria_labelledby_joins_multiple_ids(
    fixture_server: BoundFixtureServer, exec_context: ToolExecutionContext
) -> None:
    """``aria-labelledby`` is a space-separated list; the snapshot must
    concatenate the referenced labels in document order. A naive
    ``getElementById(attr)`` would silently drop all but the first label."""
    result = await browser_open_tool(exec_context, fixture_server.url + "/labelledby")
    data = result.get_data()
    assert isinstance(data, dict)

    def walk(nodes: list[SnapshotNode]) -> SnapshotNode | None:
        for node in nodes:
            if node["role"] == "textbox":
                return node
            children = node.get("children", [])
            if children:
                found = walk(children)
                if found is not None:
                    return found
        return None

    textbox = walk(data["roots"])
    assert textbox is not None, f"no textbox in snapshot:\n{result.get_text()}"
    assert textbox["name"] == "Quantity lbs"


def _first_ref_for(snap: ToolResult, role: str) -> str:
    """Return the first ref whose node has the given ``role``.

    Walks the structured ``roots`` tree from the snapshot ``data`` rather than
    parsing TOON text — refs aren't stable across runs, and tree traversal is
    both simpler and more reliable than regexing serialized output.
    """
    data = snap.get_data()
    assert isinstance(data, dict), f"expected dict snapshot data, got {type(data)}"
    roots = data.get("roots", [])

    def walk(nodes: list[SnapshotNode]) -> str | None:
        for node in nodes:
            if node["role"] == role:
                return node["ref"]
            children = node.get("children", [])
            if children:
                found = walk(children)
                if found is not None:
                    return found
        return None

    found = walk(roots)
    if found is None:
        raise AssertionError(
            f"No ref with role={role!r} found in snapshot:\n{snap.get_text()}"
        )
    return found


def _ref_for_name(snap: ToolResult, name: str) -> str:
    """Return the ref of the first node whose accessible name is ``name``."""
    data = snap.get_data()
    assert isinstance(data, dict), f"expected dict snapshot data, got {type(data)}"

    def walk(nodes: list[SnapshotNode]) -> str | None:
        for node in nodes:
            if node["name"] == name and node["role"] == "textbox":
                return node["ref"]
            found = walk(node.get("children", []))
            if found is not None:
                return found
        return None

    found = walk(data.get("roots", []))
    if found is None:
        raise AssertionError(
            f"No textbox named {name!r} in snapshot:\n{snap.get_text()}"
        )
    return found


def _ref_for_role_and_name(snap: ToolResult, role: str, name: str) -> str:
    data = snap.get_data()
    assert isinstance(data, dict)

    def walk(nodes: list[SnapshotNode]) -> str | None:
        for node in nodes:
            if node["role"] == role and node["name"] == name:
                return node["ref"]
            found = walk(node.get("children", []))
            if found is not None:
                return found
        return None

    found = walk(data.get("roots", []))
    if found is None:
        raise AssertionError(
            f"No {role} named {name!r} in snapshot:\n{snap.get_text()}"
        )
    return found


async def test_screenshot_coordinate_hover_and_click_on_non_a11y_target(
    fixture_server: BoundFixtureServer, exec_context: ToolExecutionContext
) -> None:
    """An unlabelled div missing from the a11y snapshot can be hovered and clicked via screenshot coordinates."""
    snap = await browser_open_tool(exec_context, fixture_server.url + "/interactive")
    assert "idle" in snap.get_text()

    shot = await browser_screenshot_tool(exec_context)
    shot_data = shot.get_data()
    assert isinstance(shot_data, dict)
    assert shot_data["width"] == 1024
    assert shot_data["height"] == 768
    assert shot_data["viewport_width"] == 1024.0
    assert shot_data["viewport_height"] == 768.0
    assert shot_data["device_pixel_ratio"] == 1.0

    hovered = await browser_hover_at_tool(exec_context, 320, 140)
    assert "hovered-arrow" in hovered.get_text()

    clicked = await browser_click_at_tool(exec_context, 320, 140)
    assert "clicked-arrow" in clicked.get_text()


async def test_coordinate_click_maps_dpr_2_screenshot_to_css_viewport(
    fixture_server: BoundFixtureServer, exec_context: ToolExecutionContext
) -> None:
    """At devicePixelRatio=2, screenshot pixel coordinates divide cleanly to CSS viewport coords."""
    session = await get_browser_session(exec_context)
    session.playwright = await async_playwright().start()
    session.browser = await launch_stealth_browser(session.playwright, headless=True)
    session.context = await session.browser.new_context(
        viewport={"width": 1024, "height": 768},
        device_scale_factor=2,
    )
    session.page = await session.context.new_page()

    await browser_open_tool(exec_context, fixture_server.url + "/interactive")
    shot = await browser_screenshot_tool(exec_context)
    shot_data = shot.get_data()
    assert isinstance(shot_data, dict)
    assert shot_data["width"] == 2048
    assert shot_data["height"] == 1536
    assert shot_data["viewport_width"] == 1024.0
    assert shot_data["viewport_height"] == 768.0
    assert shot_data["device_pixel_ratio"] == 2.0

    # Target div is at CSS (300..340, 120..160), so its center in the 2x PNG is (640, 280).
    clicked = await browser_click_at_tool(exec_context, 640, 280)
    assert "clicked-arrow" in clicked.get_text()


async def test_scroll_page_and_container_and_click_after_scroll(
    fixture_server: BoundFixtureServer, exec_context: ToolExecutionContext
) -> None:
    """Scrolling works without a screenshot, invalidates stale screenshots, and viewport clicks work when scrolled."""
    await browser_open_tool(exec_context, fixture_server.url + "/interactive")

    # Scroll the inner scrollable container using coordinates from a screenshot.
    await browser_screenshot_tool(exec_context)
    box_scrolled = await browser_scroll_tool(
        exec_context, "down", amount=250, x=700, y=180
    )
    assert "box:0" not in box_scrolled.get_text()

    # After scrolling, coordinate clicks must refuse until a fresh screenshot is taken.
    stale_click = await browser_click_at_tool(exec_context, 320, 140)
    stale_data = stale_click.get_data()
    assert isinstance(stale_data, dict)
    assert stale_data["error"] == "no_valid_screenshot"

    # Scroll the main document down to bring #lower-icon (top: 1200px) into the viewport.
    await browser_hover_at_tool(exec_context, 0, 0)
    await browser_scroll_tool(exec_context, "down", amount=1000)
    shot = await browser_screenshot_tool(exec_context)
    shot_data = shot.get_data()
    assert isinstance(shot_data, dict)
    scroll_y = float(shot_data["scroll_y"])
    assert scroll_y >= 500.0

    # #lower-icon is at document (200..260, 1200..1240), so viewport y is (1220 - scroll_y).
    viewport_y = 1220.0 - scroll_y
    assert 0.0 < viewport_y < 768.0
    clicked = await browser_click_at_tool(exec_context, 230.0, viewport_y)
    assert "clicked-lower" in clicked.get_text()


async def test_hover_by_ref_pointer_away_and_escape_dismiss_overlays(
    fixture_server: BoundFixtureServer, exec_context: ToolExecutionContext
) -> None:
    """Hover by ref opens an overlay; moving pointer away and pressing Escape dismiss overlays without browser_exec."""
    snap = await browser_open_tool(exec_context, fixture_server.url + "/overlays")
    assert "Modal open" in snap.get_text()

    # Dismiss the modal with Escape.
    after_esc = await browser_press_key_tool(exec_context, "Escape")
    assert "modal-closed" in after_esc.get_text()
    assert "Modal open" not in after_esc.get_text()

    # Trigger a full-screen hover preview by hovering the card button ref.
    card_ref = _ref_for_role_and_name(after_esc, "button", "Hover card")
    download_ref = _ref_for_role_and_name(after_esc, "button", "Download 3MF")
    after_hover = await browser_hover_tool(exec_context, card_ref)
    assert "overlay-shown" in after_hover.get_text()
    assert "Preview overlay active" in after_hover.get_text()

    # Move pointer away to (0, 0) (no prior screenshot required) to clear the overlay, then click.
    after_away = await browser_hover_at_tool(exec_context, 0, 0)
    assert "overlay-dismissed" in after_away.get_text()
    assert "Preview overlay active" not in after_away.get_text()

    after_click = await browser_click_tool(exec_context, download_ref)
    assert "downloaded" in after_click.get_text()
