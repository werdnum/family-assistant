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

from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from family_assistant.tools.browser_dom import (
    browser_click_tool,
    browser_exec_tool,
    browser_extract_tool,
    browser_fill_tool,
    browser_open_tool,
    browser_screenshot_tool,
    browser_select_tool,
    browser_snapshot_tool,
    browser_wait_tool,
)
from family_assistant.tools.browser_session import (
    close_browser_session,
    get_browser_session,
)
from family_assistant.tools.types import ToolExecutionContext

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
    """A minimally-mocked ToolExecutionContext with a unique conversation id.

    The browser_dom tools only touch ``conversation_id``, which keys the
    per-conversation ``BrowserSession``. Using the test's nodeid keeps each
    test's session isolated.
    """
    return MagicMock(
        spec=ToolExecutionContext,
        conversation_id=f"browser-dom-test-{request.node.nodeid}",
    )


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
    input_ref = _first_ref_for(snap.get_text(), role="textbox")

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
    select_ref = _first_ref_for(snap.get_text(), role="combobox")

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
    link_ref = _first_ref_for(snap.get_text(), role="link")

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


async def test_browser_exec_clears_refs_after_dom_mutation(
    fixture_server: BoundFixtureServer, exec_context: ToolExecutionContext
) -> None:
    """After browser_exec mutates the DOM, refs from the last snapshot are
    invalidated — the agent must re-snapshot before clicking/filling."""
    await browser_open_tool(exec_context, fixture_server.url + "/")
    await browser_snapshot_tool(exec_context)
    session = await get_browser_session(exec_context)
    assert session.ref_cache, "snapshot should have populated ref cache"

    await browser_exec_tool(exec_context, code="return document.title")
    assert session.ref_cache == {}


async def test_unknown_ref_raises_clear_error(
    fixture_server: BoundFixtureServer, exec_context: ToolExecutionContext
) -> None:
    await browser_open_tool(exec_context, fixture_server.url + "/")
    with pytest.raises(ValueError, match="Unknown ref 'e999'"):
        await browser_click_tool(exec_context, ref="e999")


def _first_ref_for(snapshot_text: str, role: str) -> str:
    """Return the first ref whose line's role matches ``role``.

    The TOON lines look like ``  [e12] textbox "Search"``; this util extracts
    the first ref for the requested role so tests don't have to hard-code
    ref numbers (which aren't stable across runs).
    """
    for line in snapshot_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("["):
            continue
        close = stripped.find("]")
        if close == -1:
            continue
        after = stripped[close + 1 :].strip()
        if after.startswith(role):
            return stripped[1:close]
    raise AssertionError(
        f"No ref with role={role!r} found in snapshot:\n{snapshot_text}"
    )
