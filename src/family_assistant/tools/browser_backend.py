"""Pluggable backend for the semantic DOM browser tools.

The :mod:`browser_dom` tools used to drive a local headless Playwright page
directly. To allow the *same* tools to run against an external
``browser-server`` (handoff) session — a browser that can be transferred to a
human via noVNC — the page operations are funnelled through a
:class:`BrowserBackend`:

- :class:`LocalPlaywrightBackend` wraps the shared in-process
  :class:`~family_assistant.tools.browser_session.BrowserSession` (the previous
  behavior, and the one shared with the visual Computer Use profile).
- :class:`RemoteBrowserBackend` is an httpx client for ``browser-server``'s
  ``/v1/sessions/*`` agent API. The remote worker runs the *same* accessibility
  walker server-side and returns the same :class:`Snapshot` JSON, so ref
  handling and TOON rendering on this side are identical.

The backend is selected per conversation by :func:`get_browser_backend`: when
``browser_handoff_config`` is enabled and the active profile is handoff-capable,
a remote backend is used; otherwise the local backend is used. When the remote
integration is disabled (the default), none of the remote code paths are
reachable and behavior is byte-for-byte the previous local path.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import os
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    Protocol,
    cast,
    get_args,
    runtime_checkable,
)

import httpx
from rebrowser_playwright.async_api import Error as PlaywrightError

from family_assistant.tools.browser_session import (
    BrowserSession,
    close_browser_session,
    get_browser_session,
)

if TYPE_CHECKING:
    from rebrowser_playwright.async_api import Locator, Page

    from family_assistant.config_models import BrowserHandoffConfig
    from family_assistant.tools.browser_dom import Snapshot
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

# Agent-command payloads and browser-server JSON responses are genuinely
# arbitrary JSON shapes, so a typed structure would be misleading here.
# ast-grep-ignore: no-dict-any - heterogeneous browser-server JSON payloads
JsonDict = dict[str, Any]

LoadState = Literal["load", "domcontentloaded", "networkidle"]
_VALID_LOAD_STATES: tuple[LoadState, ...] = get_args(LoadState)

# In-page accessibility walker and ref resolver, shared VERBATIM with
# browser-server (``browser_handoff_service.runtime``). A unit test asserts the
# two copies are equal, so edit both or neither.
#
# A node keeps its ref across snapshots while its role and accessible name are
# unchanged; anything else is stamped with a fresh number taken from the
# caller-supplied counter, which the conversation's ``BrowserSession`` holds.
# The ref ``e12`` always resolves to ``[data-fa-ref="e12"]``, and
# ``CHECK_REF_JS`` decides whether it still names the node it was issued for.

_WALKER_HELPERS_JS = r"""
  const REF_ATTR = 'data-fa-ref';
  const ROLE_ATTR = 'data-fa-role';
  const NAME_ATTR = 'data-fa-name';
  // A control whose value must never be copied into a snapshot: every password
  // input, plus any element an autofill has touched (which keeps the stamp when
  // a "show password" toggle changes the input's type).
  const PROTECTED_ATTR = 'data-fa-protected';
  const REF_PATTERN = /^e[0-9]+$/;

  function isProtected(el) {
    if (el.hasAttribute && el.hasAttribute(PROTECTED_ATTR)) return true;
    return el.tagName === 'INPUT' && (el.getAttribute('type') || '').toLowerCase() === 'password';
  }

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
  // Button-like inputs are labelled by their value, which is also what a page
  // changes when it repurposes one, so identity has to include it.
  const BUTTON_INPUT_TYPES = new Set(['submit', 'button', 'reset']);
  const NAME_FROM_CONTENT = new Set([
    'A', 'BUTTON', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6',
    'P', 'LI', 'SPAN', 'LABEL', 'OPTION', 'TD', 'TH', 'CAPTION',
  ]);

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
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const parts = [];
      for (const id of labelledBy.trim().split(/\s+/)) {
        const target = id && document.getElementById(id);
        if (target) parts.push(target.textContent.trim());
      }
      if (parts.length) return parts.join(' ');
    }
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    if (el.id) {
      const lbl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lbl) return lbl.textContent.trim();
    }
    const parentLabel = el.closest && el.closest('label');
    if (parentLabel && parentLabel !== el) return parentLabel.textContent.trim();
    if (el.getAttribute('alt')) return el.getAttribute('alt').trim();
    if (el.getAttribute('title')) return el.getAttribute('title').trim();
    if (el.getAttribute('placeholder')) return el.getAttribute('placeholder').trim();
    if (el.tagName === 'INPUT' && BUTTON_INPUT_TYPES.has((el.getAttribute('type') || '').toLowerCase())) {
      return (el.value || '').trim();
    }
    if (!NAME_FROM_CONTENT.has(el.tagName)) return '';
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

  // Why a snapshot taken now would not list ``el`` under its stamped ref, or
  // null when it would. This is the one eligibility predicate the walker and
  // the resolver share: the walker lists exactly the nodes for which it is
  // null, and an action resolves a ref exactly when it is null.
  function ineligible(el) {
    let inBody = false;
    for (let n = el; n; n = n.parentElement) {
      if (n.nodeType !== 1 || !isVisible(n)) return 'hidden';
      if (n === document.body) { inBody = true; break; }
    }
    if (!inBody) return 'missing';
    const role = interesting(el);
    if (role === null) return 'changed';
    if (role !== el.getAttribute(ROLE_ATTR)) return 'changed';
    if (accName(el) !== el.getAttribute(NAME_ATTR)) return 'changed';
    return null;
  }
"""

# ``(nextRef) => snapshot``. ``nextRef`` is the lowest number the caller permits
# for a fresh ref; the result's ``next_ref`` is the counter after this walk.
SNAPSHOT_JS = (
    "(nextRef) => {"
    + _WALKER_HELPERS_JS
    + r"""
  let highest = 0;
  // How many elements carry each stamp, hidden ones included: a page that
  // clones a stamped node leaves two, and a ref shared by two nodes is no
  // ref at all, so neither keeps it.
  const carriers = new Map();
  for (const el of document.querySelectorAll('[' + REF_ATTR + ']')) {
    const stamped = el.getAttribute(REF_ATTR) || '';
    if (!REF_PATTERN.test(stamped)) continue;
    carriers.set(stamped, (carriers.get(stamped) || 0) + 1);
    const n = parseInt(stamped.slice(1), 10);
    if (n > highest) highest = n;
  }
  let counter = Math.max(Math.floor(Number(nextRef)) || 1, highest + 1);
  const issued = new Set();

  function refFor(el, role, name) {
    const existing = el.getAttribute(REF_ATTR) || '';
    if (
      REF_PATTERN.test(existing) &&
      carriers.get(existing) === 1 &&
      !issued.has(existing) &&
      el.getAttribute(ROLE_ATTR) === role &&
      el.getAttribute(NAME_ATTR) === name
    ) {
      issued.add(existing);
      return existing;
    }
    const ref = 'e' + (counter++);
    el.setAttribute(REF_ATTR, ref);
    el.setAttribute(ROLE_ATTR, role);
    el.setAttribute(NAME_ATTR, name);
    issued.add(ref);
    return ref;
  }

  let listed = 0;

  function walk(el, out) {
    if (el.nodeType !== 1) return;
    if (!isVisible(el)) return;
    const role = interesting(el);
    if (role) {
      const name = accName(el);
      const ref = refFor(el, role, name);
      listed += 1;
      const node = { ref, role, name };
      const href = el.getAttribute('href');
      if (href) node.href = href;
      if (isProtected(el)) {
        // Stamp on sight so the control stays protected after a type change, and
        // report only whether it holds something — never what.
        if (!el.hasAttribute(PROTECTED_ATTR)) el.setAttribute(PROTECTED_ATTR, '1');
        node.value_masked = true;
        node.has_value = typeof el.value === 'string' && el.value.length > 0;
      } else {
        const value = el.value;
        if (typeof value === 'string' && value) node.value = value;
      }
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
    elements: listed,
    next_ref: counter,
    roots,
  };
}
"""
)

# ``(ref) => {ok: true} | {ok: false, cause}``. ``cause`` is ``missing`` (no node
# carries the ref), ``hidden`` (the node or an ancestor is not visible) or
# ``changed`` (the node's role or name differs from what was snapshotted).
CHECK_REF_JS = (
    "(ref) => {"
    + _WALKER_HELPERS_JS
    + r"""
  if (typeof ref !== 'string' || !REF_PATTERN.test(ref)) return { ok: false, cause: 'missing' };
  const carriers = document.querySelectorAll('[' + REF_ATTR + '="' + ref + '"]');
  if (carriers.length !== 1) return { ok: false, cause: 'missing' };
  const el = carriers[0];
  const cause = ineligible(el);
  if (cause !== null) return { ok: false, cause };
  return { ok: true };
}
"""
)


def _coerce_load_state(state: str) -> LoadState:
    """Validate and narrow a runtime string to a Playwright load state literal."""
    for candidate in _VALID_LOAD_STATES:
        if candidate == state:
            return candidate
    raise ValueError(
        f"Invalid load state {state!r}; expected one of {_VALID_LOAD_STATES}"
    )


def wrap_exec_code(code: str) -> str:
    """Wrap user-provided JS so ``page.evaluate`` can run it uniformly.

    Playwright treats a function-shaped string as callable and evaluates a bare
    expression as its value. We want both styles — ``document.title``
    (expression) and ``return document.title`` (statement body) — to work.
    """
    stripped = code.strip()
    if not stripped:
        return "async () => null"
    if stripped.startswith(("(", "async ", "function ")):
        return stripped
    if stripped.startswith("{"):
        return f"async () => {stripped}"
    looks_like_statements = "return " in stripped or ";" in stripped or "\n" in stripped
    if looks_like_statements:
        return f"async () => {{ {stripped} }}"
    return f"async () => ({stripped})"


class BrowserBackendError(RuntimeError):
    """Raised when a backend operation fails (remote HTTP error, JS error, …)."""


class BrowserSessionGoneError(BrowserBackendError):
    """The server expired or forgot the bound browser session."""


class HandoffUnavailableError(BrowserBackendError):
    """Raised when a human handoff is requested but no remote backend is active."""


class StaleRefError(BrowserBackendError):
    """Raised when a ref no longer names the node it was issued for.

    The page decides this: a snapshot taken at that moment would not list the
    node under that ref, because it was removed (``missing``), is no longer
    visible (``hidden``), or its role or accessible name changed (``changed``).
    """

    def __init__(self, ref: str, cause: str, reason: str | None = None) -> None:
        self.ref = ref
        self.cause = cause
        super().__init__(
            reason
            or (
                f"ref {ref} is no longer on the page as snapshotted; the page "
                f"has changed since the last snapshot"
            )
        )


@runtime_checkable
class BrowserBackend(Protocol):
    """Page-level operations shared by semantic DOM and visual computer-use tools.

    Ref actions take the ref itself (``e12``) rather than a selector: the
    backend checks in the page that the ref still names the node it was issued
    for — raising :class:`StaleRefError` when it does not — before acting on
    ``[data-fa-ref="e12"]``. ``raw_snapshot`` takes the conversation's ref
    counter and reports the advanced one back as ``next_ref``.

    The ``mouse_*`` / ``keyboard_*`` / ``go_back`` / ``go_forward`` methods are
    used by the visual (Computer Use) profile so it can share the same remote
    browser session instead of opening a separate local tab.
    """

    @property
    def current_url(self) -> str: ...

    @property
    def screen_width(self) -> int: ...

    @property
    def screen_height(self) -> int: ...

    async def goto(self, url: str) -> None: ...

    async def raw_snapshot(self, next_ref: int) -> Snapshot: ...

    async def settle(self, timeout_ms: int = 5000) -> None: ...

    async def click(self, ref: str) -> None: ...

    async def fill(self, ref: str, text: str, submit: bool) -> None: ...

    async def select(self, ref: str, value: str) -> None: ...

    async def wait(self, selector: str | None, state: str, timeout_ms: int) -> None: ...

    async def extract_html(self, selector: str | None) -> str: ...

    async def screenshot_png(self) -> bytes: ...

    # ast-grep-ignore: no-dict-any - JS return values are genuinely arbitrary JSON
    async def evaluate(self, code: str) -> Any: ...  # noqa: ANN401

    async def mouse_click(
        self, x: float, y: float, *, button: str = "left", click_count: int = 1
    ) -> None: ...

    async def mouse_move(self, x: float, y: float) -> None: ...

    async def mouse_down(self) -> None: ...

    async def mouse_up(self) -> None: ...

    async def mouse_wheel(self, delta_x: float, delta_y: float) -> None: ...

    async def keyboard_type(self, text: str) -> None: ...

    async def keyboard_press(self, keys: str) -> None: ...

    async def keyboard_down(self, key: str) -> None: ...

    async def keyboard_up(self, key: str) -> None: ...

    async def go_back(self) -> None: ...

    async def go_forward(self) -> None: ...

    async def request_handoff(
        self,
        *,
        reason: str,
        handoff_note: str,
        expected_origin: str | None,
        allow_resume: bool = False,
    ) -> JsonDict: ...

    async def claim_handback(
        self, session_id: str, handback_token: str
    ) -> JsonDict: ...

    async def close(self) -> None: ...


class LocalPlaywrightBackend:
    """Backend wrapping the shared in-process Playwright ``BrowserSession``."""

    def __init__(self, session: BrowserSession) -> None:
        self._session = session

    @property
    def current_url(self) -> str:
        page = self._session.page
        return page.url if page is not None else ""

    @property
    def screen_width(self) -> int:
        return self._session.screen_width

    @property
    def screen_height(self) -> int:
        return self._session.screen_height

    async def _page(self) -> Page:
        return await self._session.ensure_page()

    async def _locator_for_ref(self, ref: str) -> Locator:
        """Check ``ref`` in the page and return a locator for its node.

        The check runs the walker's own eligibility predicate, so a ref
        resolves exactly when a snapshot taken now would list that node under
        it — and fails immediately rather than waiting out Playwright's
        actionability timeout when it would not.
        """
        page = await self._page()
        raw = await page.evaluate(CHECK_REF_JS, ref)
        checked = cast("JsonDict", raw)
        if not checked.get("ok"):
            raise StaleRefError(ref=ref, cause=str(checked.get("cause", "missing")))
        return page.locator(f'[data-fa-ref="{ref}"]')

    async def goto(self, url: str) -> None:
        page = await self._page()
        await page.goto(url)

    async def raw_snapshot(self, next_ref: int) -> Snapshot:
        page = await self._page()
        raw = await page.evaluate(SNAPSHOT_JS, next_ref)
        return cast("Snapshot", raw)

    async def settle(self, timeout_ms: int = 5000) -> None:
        page = await self._page()
        with contextlib.suppress(PlaywrightError):
            await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)

    async def click(self, ref: str) -> None:
        locator = await self._locator_for_ref(ref)
        await locator.click()

    async def fill(self, ref: str, text: str, submit: bool) -> None:
        locator = await self._locator_for_ref(ref)
        await locator.fill(text)
        if submit:
            await locator.press("Enter")

    async def select(self, ref: str, value: str) -> None:
        locator = await self._locator_for_ref(ref)
        await locator.select_option(value)

    async def wait(self, selector: str | None, state: str, timeout_ms: int) -> None:
        page = await self._page()
        if selector:
            await page.wait_for_selector(selector, timeout=timeout_ms)
        else:
            await page.wait_for_load_state(
                _coerce_load_state(state), timeout=timeout_ms
            )

    async def extract_html(self, selector: str | None) -> str:
        page = await self._page()
        if selector:
            return await page.locator(selector).inner_html()
        return await page.content()

    async def screenshot_png(self) -> bytes:
        page = await self._page()
        return await page.screenshot(type="png")

    # ast-grep-ignore: no-dict-any - JS return values are genuinely arbitrary JSON
    async def evaluate(self, code: str) -> Any:  # noqa: ANN401
        page = await self._page()
        try:
            return await page.evaluate(wrap_exec_code(code))
        except PlaywrightError as exc:
            raise BrowserBackendError(str(exc)) from exc

    async def mouse_click(
        self, x: float, y: float, *, button: str = "left", click_count: int = 1
    ) -> None:
        page = await self._page()
        # Validate button parameter for typing purposes
        if button not in {"left", "middle", "right"}:
            raise BrowserBackendError(f"Invalid mouse button: {button!r}")
        await page.mouse.click(
            x,
            y,
            button=cast("Literal['left', 'middle', 'right']", button),
            click_count=click_count,
        )
        with contextlib.suppress(PlaywrightError):
            await page.wait_for_load_state("domcontentloaded", timeout=2000)

    async def mouse_move(self, x: float, y: float) -> None:
        page = await self._page()
        await page.mouse.move(x, y)

    async def mouse_down(self) -> None:
        page = await self._page()
        await page.mouse.down()

    async def mouse_up(self) -> None:
        page = await self._page()
        await page.mouse.up()

    async def mouse_wheel(self, delta_x: float, delta_y: float) -> None:
        page = await self._page()
        await page.mouse.wheel(delta_x, delta_y)

    async def keyboard_type(self, text: str) -> None:
        page = await self._page()
        await page.keyboard.type(text)

    async def keyboard_press(self, keys: str) -> None:
        page = await self._page()
        await page.keyboard.press(keys)
        with contextlib.suppress(PlaywrightError):
            await page.wait_for_load_state("domcontentloaded", timeout=2000)

    async def keyboard_down(self, key: str) -> None:
        page = await self._page()
        await page.keyboard.down(key)

    async def keyboard_up(self, key: str) -> None:
        page = await self._page()
        await page.keyboard.up(key)

    async def go_back(self) -> None:
        page = await self._page()
        await page.go_back()

    async def go_forward(self) -> None:
        page = await self._page()
        await page.go_forward()

    async def request_handoff(
        self,
        *,
        reason: str,
        handoff_note: str,
        expected_origin: str | None,
        allow_resume: bool = False,
    ) -> JsonDict:
        raise HandoffUnavailableError(
            "Human browser handoff requires browser_handoff_config to be enabled "
            "(no remote browser-server is configured)."
        )

    async def claim_handback(self, session_id: str, handback_token: str) -> JsonDict:
        raise HandoffUnavailableError(
            "claim_handback requires browser-server integration"
        )

    async def close(self) -> None:
        await self._session.close()


# browser-server default viewport — must match DEFAULT_DISPLAY_WIDTH/HEIGHT in runtime.py
_REMOTE_VIEWPORT_WIDTH = 1280
_REMOTE_VIEWPORT_HEIGHT = 720


@dataclass(frozen=True, slots=True)
class AuthenticatedSessionSpec:
    """Everything trusted orchestration pins onto an authenticated session.

    Assembled from the site's configuration, never from model-supplied
    arguments: the jar, the complete origin set browser-server must confine to,
    and the one Keychute alias this session's fills may ask for.
    """

    site_id: str
    jar_id: str | None
    confine_origins: frozenset[str]
    credential_alias: str | None


class AuthenticatedSessionMismatchError(BrowserBackendError):
    """browser-server created a session that is not the one we asked for.

    Raised when the created session's effective origin set or jar generation
    does not match what the configuration named. The session is closed and the
    run fails rather than proceeding against a wider boundary than the operator
    declared.
    """


class AuthenticatedSessionUnavailableError(BrowserBackendError):
    """An authenticated session was lost and must not be silently replaced.

    The ordinary backend replaces a lost-lease or expired session with a fresh
    one on navigate so a conversation is never wedged. For an authenticated-site
    run that recovery would be an unconfined escape hatch opened mid-handoff by
    page-controlled input, so every command fails closed with this instead.
    """


class RemoteBrowserBackend:
    """Backend that drives a remote ``browser-server`` session over HTTP."""

    def __init__(
        self,
        config: BrowserHandoffConfig,
        conversation_id: str,
        client: httpx.AsyncClient | None = None,
        timezone_id: str | None = None,
        authenticated: AuthenticatedSessionSpec | None = None,
    ) -> None:
        if not config.service_url:
            raise BrowserBackendError(
                "browser_handoff_config.enabled is set but service_url is missing"
            )
        self._config = config
        self._conversation_id = conversation_id
        self._base_url = config.service_url.rstrip("/")
        # IANA timezone (e.g. "Australia/Sydney") forwarded to browser-server so the
        # remote browser context reports the user's local time to in-page JS.
        self._timezone_id = timezone_id
        self._session_id: str | None = None
        # ``client`` is an injection seam for tests (e.g. httpx.MockTransport).
        self._client = client or httpx.AsyncClient(timeout=config.timeout_seconds)
        self._last_url: str = ""
        self._authenticated = authenticated

    @property
    def current_url(self) -> str:
        return self._last_url

    @property
    def authenticated_spec(self) -> AuthenticatedSessionSpec | None:
        """The authenticated-site pin on this backend, if it has one."""
        return self._authenticated

    @property
    def session_id(self) -> str | None:
        """The live browser-server session id, or ``None`` before one exists."""
        return self._session_id

    def _headers(self) -> dict[str, str]:
        auth = self._config.auth
        if auth.type == "none" or not auth.token_env:
            return {}
        token = os.environ.get(auth.token_env)
        if not token:
            raise BrowserBackendError(
                f"browser_handoff_config auth token env {auth.token_env!r} is not set"
            )
        if auth.type == "api_key":
            return {auth.header_name: token}
        return {auth.header_name: f"Bearer {token}"}

    async def get_jar(self, jar_id: str) -> JsonDict:
        """Read a saved login's non-secret status record.

        Carries ``invalidated_at`` and the tombstone marker, which is how a
        deliberate human revocation is told apart from a session that merely
        lapsed -- the distinction the whole autofill routing turns on.
        """
        resp = await self._client.get(
            f"{self._base_url}/v1/jars/{jar_id}",
            headers=self._headers(),
        )
        if resp.status_code == 404:
            return {"jar_id": jar_id, "missing": True}
        self._raise_for_status(resp, f"read jar {jar_id}")
        return cast("JsonDict", resp.json())

    async def probe_jar(self, jar_id: str) -> JsonDict:
        """Ask browser-server whether a saved login is still usable."""
        resp = await self._client.post(
            f"{self._base_url}/v1/jars/{jar_id}/probe",
            headers=self._headers(),
            json={},
        )
        if resp.status_code == 404:
            return {"jar_id": jar_id, "missing": True, "fresh": False}
        self._raise_for_status(resp, f"probe jar {jar_id}")
        return cast("JsonDict", resp.json())

    async def start_authenticated_session(
        self, *, expected_jar_generation: int | None = None
    ) -> str:
        """Create the confined session this backend is pinned to, and verify it.

        The session-creation call is the chokepoint the design puts the origin
        check at: browser-server answers with the effective set it will actually
        enforce, and a set that is not exactly the configured one -- a jar whose
        saved navigation allowlist reaches somewhere the configuration does not
        declare, say -- fails the run instead of widening it.
        """
        spec = self._authenticated
        if spec is None:
            raise BrowserBackendError(
                "start_authenticated_session called on a backend with no "
                "authenticated-site pin"
            )
        if self._session_id is not None:
            raise BrowserBackendError(
                "this backend already has a browser-server session; an "
                "authenticated session is created once and never replaced"
            )
        payload: JsonDict = {
            "conversation_id": self._conversation_id,
            "interface_type": "research",
            "initial_owner": "agent",
            "authenticated_site": True,
            "confine_origins": sorted(spec.confine_origins),
        }
        if spec.jar_id is not None:
            payload["jar_id"] = spec.jar_id
        if spec.credential_alias is not None:
            payload["credential_alias"] = spec.credential_alias
        if self._timezone_id:
            payload["timezone_id"] = self._timezone_id
        resp = await self._client.post(
            f"{self._base_url}/v1/sessions",
            headers=self._headers(),
            json=payload,
        )
        self._raise_for_status(resp, "create authenticated session")
        body = cast("JsonDict", resp.json())
        session_id = str(body["session_id"])
        self._session_id = session_id
        try:
            self._verify_created_session(body, expected_jar_generation)
        except AuthenticatedSessionMismatchError:
            await self.close()
            raise
        return session_id

    def _verify_created_session(
        self, body: JsonDict, expected_jar_generation: int | None
    ) -> None:
        """Fail closed unless the created session is the one we asked for."""
        spec = self._authenticated
        assert spec is not None
        if body.get("authenticated_site") is not True:
            raise AuthenticatedSessionMismatchError(
                f"browser-server did not mark the session for site "
                f"{spec.site_id!r} as an authenticated-site session"
            )
        effective = _origin_set(body.get("confine_origins"))
        if effective != spec.confine_origins:
            raise AuthenticatedSessionMismatchError(
                f"browser-server confined the session for site {spec.site_id!r} "
                f"to {sorted(effective)}, but the configuration declares "
                f"{sorted(spec.confine_origins)}"
            )
        jar_reachable = _origin_set(body.get("jar_origins")) | _origin_set(
            body.get("jar_nav_allowlist")
        )
        if not jar_reachable <= spec.confine_origins:
            raise AuthenticatedSessionMismatchError(
                f"the jar bound to site {spec.site_id!r} reaches origins the "
                f"configuration does not declare: "
                f"{sorted(jar_reachable - spec.confine_origins)}"
            )
        generation = body.get("jar_generation")
        if expected_jar_generation is not None and generation != (
            expected_jar_generation
        ):
            raise AuthenticatedSessionMismatchError(
                f"the jar bound to site {spec.site_id!r} changed between the "
                f"status read (generation {expected_jar_generation}) and session "
                f"creation (generation {generation!r})"
            )
        if body.get("credential_alias") != spec.credential_alias:
            raise AuthenticatedSessionMismatchError(
                f"browser-server pinned credential alias "
                f"{body.get('credential_alias')!r} to the session for site "
                f"{spec.site_id!r}, not the configured {spec.credential_alias!r}"
            )

    def adopt_session(self, session_id: str) -> None:
        """Bind this backend to a session that already exists.

        How a parked session is picked up again: the run that resumes it
        rebuilds the backend from the same configuration and adopts the id
        recorded on the delegation run, rather than creating a second session.
        """
        self._session_id = session_id

    async def session_state(self) -> JsonDict:
        """Read the live session record, including its handover state.

        The handback token that reclaims a lease is minted by browser-server
        only when the human finishes, so it is read from here rather than
        carried through the conversation.
        """
        session_id = self._session_id
        if session_id is None:
            raise BrowserBackendError("no browser-server session to read")
        resp = await self._client.get(
            f"{self._base_url}/v1/sessions/{session_id}",
            headers=self._headers(),
        )
        if self._is_session_gone_response(resp):
            raise BrowserSessionGoneError("the browser session is gone")
        self._raise_for_status(resp, "read session")
        return cast("JsonDict", resp.json())

    async def autofill(
        self,
        *,
        step_key: str,
        fields: list[JsonDict] | None = None,
        wait_seconds: float | None = None,
        context: JsonDict | None = None,
    ) -> JsonDict:
        """Ask browser-server to fill the session's pinned credential.

        The outcome comes back as a 200 with a typed status, so a policy result
        never has to be inferred from an HTTP code. No secret crosses this
        boundary in either direction.
        """
        session_id = await self._ensure_session()
        payload: JsonDict = {"step_key": step_key}
        if fields is not None:
            payload["fields"] = fields
        if wait_seconds is not None:
            payload["wait_seconds"] = wait_seconds
        if context is not None:
            payload["context"] = context
        resp = await self._client.post(
            f"{self._base_url}/v1/sessions/{session_id}/autofill",
            headers=self._headers(),
            json=payload,
        )
        if self._is_session_gone_response(resp) or self._is_lease_lost_response(resp):
            raise self._session_lost_error("autofill", session_id)
        self._raise_for_status(resp, "autofill")
        return cast("JsonDict", resp.json())

    async def report_autofill_outcome(self, outcome: str) -> JsonDict:
        """Latch a login outcome on the session; a bad password refuses later fills."""
        session_id = await self._ensure_session()
        resp = await self._client.post(
            f"{self._base_url}/v1/sessions/{session_id}/autofill/outcome",
            headers=self._headers(),
            json={"outcome": outcome},
        )
        if self._is_session_gone_response(resp) or self._is_lease_lost_response(resp):
            raise self._session_lost_error("autofill outcome", session_id)
        self._raise_for_status(resp, "autofill outcome")
        return cast("JsonDict", resp.json())

    async def _ensure_session(self) -> str:
        if self._session_id is not None:
            return self._session_id
        if self._authenticated is not None:
            raise AuthenticatedSessionUnavailableError(
                f"the authenticated browser session for site "
                f"{self._authenticated.site_id!r} is no longer available. It is "
                "never replaced automatically, because a fresh session would "
                "not carry the login this task was authorised for. The run ends "
                "here; start it again."
            )
        payload: JsonDict = {
            "conversation_id": self._conversation_id,
            "interface_type": "research",
            "initial_owner": "agent",
        }
        if self._timezone_id:
            payload["timezone_id"] = self._timezone_id
        resp = await self._client.post(
            f"{self._base_url}/v1/sessions",
            headers=self._headers(),
            json=payload,
        )
        self._raise_for_status(resp, "create session")
        self._session_id = str(resp.json()["session_id"])
        return self._session_id

    def _clear_remote_session(self, session_id: str) -> None:
        logger.warning(
            "browser-server session %s is no longer available; clearing cached session",
            session_id,
        )
        self._session_id = None
        self._last_url = ""

    def _is_unknown_session_response(self, resp: httpx.Response) -> bool:
        if resp.status_code != 404:
            return False
        try:
            payload = resp.json()
        except ValueError:
            return "unknown session" in resp.text.lower()
        if not isinstance(payload, dict):
            return "unknown session" in resp.text.lower()
        detail = payload.get("detail")
        return isinstance(detail, str) and "unknown session" in detail.lower()

    def _is_session_gone_response(self, resp: httpx.Response) -> bool:
        """True when the cached session can no longer be used and must be replaced.

        Covers two server signals: 404 ``unknown session`` (the server forgot the
        session, e.g. after eviction or a restart) and 410 ``Gone`` (the session
        existed but expired or reached a terminal state). Both mean "start a new
        session". A handed-off 403 is handled separately by
        :meth:`_is_lease_lost_response` so that only ``browser_open`` (navigate)
        starts fresh, while in-flight commands surface a clear error instead.
        """
        return resp.status_code == 410 or self._is_unknown_session_response(resp)

    def _is_lease_lost_response(self, resp: httpx.Response) -> bool:
        """True when the session still exists but the agent no longer holds its lease.

        browser-server returns 403 with this detail once the session has been handed
        to a human (``handoff_requested``/``human_active``) or is parked awaiting a
        human->agent handover claim (``handover_requested``). Without this, a
        conversation that ever handed the browser off stays pinned to a session it
        can no longer drive — every later ``browser_open`` keeps hitting the same 403,
        so the browser is blocked for the rest of the conversation (and any subagent
        sharing the conversation), which is the bug this detects.

        It is deliberately distinct from an auth 403 (``agent service token
        required``): that must not reset the session. The lease-denied detail always
        contains "the lease", which the auth detail never does.
        """
        if resp.status_code != 403:
            return False
        try:
            payload = resp.json()
        except ValueError:
            return "the lease" in resp.text.lower()
        if not isinstance(payload, dict):
            return "the lease" in resp.text.lower()
        detail = payload.get("detail")
        return isinstance(detail, str) and "the lease" in detail.lower()

    def _session_lost_error(self, action: str, session_id: str) -> BrowserBackendError:
        self._clear_remote_session(session_id)
        return BrowserBackendError(
            f"browser-server {action} failed because the live browser session is "
            "not available to the agent (it may have been evicted, expired, or "
            "handed to a human). Start with browser_open to create a new browser "
            "session, or browser_claim_handback to resume a session a human handed "
            "back."
        )

    # ast-grep-ignore: no-dict-any - agent-command results are heterogeneous JSON
    async def _command(
        self, command_type: str, args: JsonDict | None = None
    ) -> JsonDict:
        session_id = await self._ensure_session()
        resp = await self._client.post(
            f"{self._base_url}/v1/sessions/{session_id}/agent-command",
            headers=self._headers(),
            json={"type": command_type, "args": args or {}},
        )
        # A gone session (eviction/expiry) or a handed-off session (the agent lost
        # the lease) both mean the cached session can't serve this command. For
        # navigate (browser_open) we transparently start a fresh session so the
        # conversation is never wedged after a handoff; other commands surface a
        # clear "start with browser_open" error instead of silently retargeting.
        if self._is_session_gone_response(resp) or self._is_lease_lost_response(resp):
            if self._authenticated is not None:
                # Never re-provisioned: see AuthenticatedSessionUnavailableError.
                raise AuthenticatedSessionUnavailableError(
                    f"browser-server refused command {command_type} on the "
                    f"authenticated session for site "
                    f"{self._authenticated.site_id!r}: the session is gone or "
                    "the agent no longer holds its lease. Authenticated sessions "
                    "are never replaced with a fresh one, so this run ends here."
                )
            if command_type != "navigate":
                raise self._session_lost_error(f"command {command_type}", session_id)
            self._clear_remote_session(session_id)
            session_id = await self._ensure_session()
            resp = await self._client.post(
                f"{self._base_url}/v1/sessions/{session_id}/agent-command",
                headers=self._headers(),
                json={"type": command_type, "args": args or {}},
            )
        self._raise_for_status(resp, f"command {command_type}")
        result = resp.json().get("result", {})
        url = result.get("url")
        if isinstance(url, str) and url:
            self._last_url = url
        return result

    def _raise_for_status(self, resp: httpx.Response, action: str) -> None:
        if resp.is_success:
            return
        detail = resp.text[:300]
        raise BrowserBackendError(
            f"browser-server {action} failed ({resp.status_code}): {detail}"
        )

    async def goto(self, url: str) -> None:
        await self._command("navigate", {"url": url})

    async def raw_snapshot(self, next_ref: int) -> Snapshot:
        result = await self._command("snapshot", {"next_ref": next_ref})
        return cast("Snapshot", result)

    @staticmethod
    def _raise_for_ref_error(result: JsonDict, ref: str) -> None:
        """Translate browser-server's ref-check failure into an exception.

        The server answers a rejected ref with HTTP 200 and an error result, so
        the distinction between "the page moved on" (retryable by the model
        after a fresh snapshot) and "that is not a ref" (a call the model
        should not have made) is made here.
        """
        if not result.get("error"):
            return
        code = result.get("code")
        if code == "invalid_ref":
            raise ValueError(
                f"Invalid ref {ref!r}. Refs look like 'e12' and come from a "
                f"snapshot; pass one exactly as the snapshot listed it."
            )
        if code == "stale_ref":
            reason = result.get("reason")
            raise StaleRefError(
                ref=str(result.get("ref", ref)),
                cause=str(result.get("cause", "missing")),
                reason=reason if isinstance(reason, str) else None,
            )
        raise BrowserBackendError(str(result.get("reason") or result.get("error")))

    async def settle(self, timeout_ms: int = 5000) -> None:
        # browser-server navigates with wait_until=domcontentloaded and settles
        # after click/type itself, so there is nothing extra to wait for here.
        return None

    async def click(self, ref: str) -> None:
        self._raise_for_ref_error(await self._command("click", {"ref": ref}), ref)

    async def fill(self, ref: str, text: str, submit: bool) -> None:
        result = await self._command("type_text", {"ref": ref, "text": text})
        self._raise_for_ref_error(result, ref)
        if submit:
            await self._command("press_key", {"key": "Enter"})

    async def select(self, ref: str, value: str) -> None:
        result = await self._command("select", {"ref": ref, "value": value})
        self._raise_for_ref_error(result, ref)

    async def wait(self, selector: str | None, state: str, timeout_ms: int) -> None:
        args: JsonDict = {"state": state, "timeout_ms": timeout_ms}
        if selector:
            args["selector"] = selector
        await self._command("wait", args)

    async def extract_html(self, selector: str | None) -> str:
        result = await self._command(
            "extract", {"selector": selector} if selector else {}
        )
        return str(result.get("html", ""))

    async def screenshot_png(self) -> bytes:
        result = await self._command("screenshot")
        encoded = result.get("image_base64")
        if not isinstance(encoded, str):
            raise BrowserBackendError(
                "browser-server screenshot returned no image data"
            )
        return base64.b64decode(encoded)

    # ast-grep-ignore: no-dict-any - JS return values are genuinely arbitrary JSON
    async def evaluate(self, code: str) -> Any:  # noqa: ANN401
        result = await self._command("exec", {"code": wrap_exec_code(code)})
        if "error" in result:
            raise BrowserBackendError(str(result["error"]))
        return result.get("result")

    @property
    def screen_width(self) -> int:
        return _REMOTE_VIEWPORT_WIDTH

    @property
    def screen_height(self) -> int:
        return _REMOTE_VIEWPORT_HEIGHT

    async def mouse_click(
        self, x: float, y: float, *, button: str = "left", click_count: int = 1
    ) -> None:
        if button != "left":
            raise BrowserBackendError(
                f"browser-server only supports left mouse clicks, not {button!r}"
            )
        if click_count != 1:
            raise BrowserBackendError(
                f"browser-server only supports single clicks, not click_count={click_count}"
            )
        await self._command("mouse_click", {"x": x, "y": y})

    async def mouse_move(self, x: float, y: float) -> None:
        await self._command("mouse_move", {"x": x, "y": y})

    async def mouse_down(self) -> None:
        await self._command("mouse_down", {})

    async def mouse_up(self) -> None:
        await self._command("mouse_up", {})

    async def mouse_wheel(self, delta_x: float, delta_y: float) -> None:
        await self._command("mouse_wheel", {"delta_x": delta_x, "delta_y": delta_y})

    async def keyboard_type(self, text: str) -> None:
        await self._command("keyboard_type", {"text": text})

    async def keyboard_press(self, keys: str) -> None:
        await self._command("keyboard_press", {"keys": keys})

    async def keyboard_down(self, key: str) -> None:
        raise BrowserBackendError("browser-server does not support keyboard_down")

    async def keyboard_up(self, key: str) -> None:
        raise BrowserBackendError("browser-server does not support keyboard_up")

    async def go_back(self) -> None:
        await self._command("navigate_back", {})

    async def go_forward(self) -> None:
        await self._command("navigate_forward", {})

    async def request_handoff(
        self,
        *,
        reason: str,
        handoff_note: str,
        expected_origin: str | None,
        allow_resume: bool = False,
    ) -> JsonDict:
        session_id = await self._ensure_session()
        payload: JsonDict = {
            "reason": reason,
            "handoff_note": handoff_note,
            # Authenticated handback is a separate server-side transition that
            # always sanitizes. Legacy resumable handoffs reject OTP/jar sessions.
            "allowed_resume": (
                "after_sanitize"
                if allow_resume and self._authenticated is None
                else "never"
            ),
        }
        if expected_origin is not None:
            payload["expected_origin"] = expected_origin
        resp = await self._client.post(
            f"{self._base_url}/v1/sessions/{session_id}/handoff",
            headers=self._headers(),
            json=payload,
        )
        if self._is_session_gone_response(resp):
            raise self._session_lost_error("handoff", session_id)
        self._raise_for_status(resp, "handoff")
        return resp.json()

    async def claim_handback(self, session_id: str, handback_token: str) -> JsonDict:
        resp = await self._client.post(
            f"{self._base_url}/v1/sessions/{session_id}/agent-claim",
            headers=self._headers(),
            json={"token": handback_token},
        )
        self._raise_for_status(resp, "agent-claim")
        self._session_id = session_id
        return resp.json()

    async def claim_handback_server_side(self, session_id: str) -> JsonDict:
        """Take an authenticated session's lease back with no token at all.

        The handback token is minted for the human who finished the step and
        the design forbids routing it through the conversation, so trusted
        orchestration has nothing to present. browser-server accepts the
        human's own handover POST as the signal instead, and this service
        token as the authority -- which is no weaker, since the same token
        created the session and drives it. The claim is refused unless the
        session is an authenticated-site one awaiting handover, and the page is
        sanitized and reopened inside the confinement set before anything here
        can observe it.
        """
        if self._authenticated is None:
            raise BrowserBackendError(
                "a token-less handback claim is only for authenticated-site "
                "sessions; an ordinary session is reclaimed with the handback "
                "token the human was shown"
            )
        resp = await self._client.post(
            f"{self._base_url}/v1/sessions/{session_id}/agent-claim",
            headers=self._headers(),
        )
        if self._is_session_gone_response(resp):
            raise BrowserSessionGoneError("the browser session is gone")
        self._raise_for_status(resp, "agent-claim")
        self._session_id = session_id
        return cast("JsonDict", resp.json())

    async def close(self) -> None:
        try:
            if self._session_id is not None:
                await self._client.post(
                    f"{self._base_url}/v1/sessions/{self._session_id}/close",
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            logger.warning("browser-server session close failed: %s", exc)
        finally:
            await self._client.aclose()
            self._session_id = None


def _origin_set(value: object) -> frozenset[str]:
    """Normalize a browser-server origin list into a comparable set."""
    if not isinstance(value, list):
        return frozenset()
    return frozenset(item.rstrip("/") for item in value if isinstance(item, str))


# Remote backends are keyed by conversation_id, mirroring the local
# BrowserSession registry so each conversation drives its own remote session.
_remote_backends: dict[str, RemoteBrowserBackend] = {}


@dataclass(slots=True)
class AuthenticatedSessionBinding:
    """One authenticated run's browser session, bound to that run.

    The conversation-keyed registry above is not the binding for an
    authenticated run: a caller can emit concurrent tool calls, and two runs
    sharing one conversation slot would overwrite each other's session or tear
    down the other's on cleanup. This is keyed to the run instead and reaches
    the delegated worker through trusted execution context, never through a
    model-visible argument.
    """

    site_id: str
    delegation_id: str
    backend: RemoteBrowserBackend
    # Autofill step signature -> the idempotency key sent to Keychute for it.
    # Held on the binding rather than derived per call so an approval_pending
    # retry of the same step replays the same request instead of opening a
    # second one, and a later step of the same login gets its own.
    step_keys: dict[str, str] = field(default_factory=dict)
    # Latched by the autofill tools and read when the run is finalized. The
    # run's outcome is derived from what actually happened rather than from the
    # worker saying the right word in its reply: a model that forgets to report
    # an outstanding approval must not turn a parked run into a completed one.
    approval_pending_request_id: str | None = None
    bad_password_recorded: bool = False
    autofill_refusal: str | None = None
    operation_failures: set[str] = field(default_factory=set)


# Keyed by the delegated run's subconversation id, which is what a running
# turn knows about itself. The visual hop is a nested delegation with its own
# subconversation, so resolution walks one parent link (see
# `resolve_authenticated_binding`) -- exactly the two hops an authenticated run
# has.
_authenticated_bindings: dict[str, AuthenticatedSessionBinding] = {}


def bind_authenticated_session(
    subconversation_id: str, binding: AuthenticatedSessionBinding
) -> None:
    """Bind an authenticated session to the run that owns it."""
    _authenticated_bindings[subconversation_id] = binding


def release_borrowed_session(subconversation_id: str) -> None:
    """Drop one borrowed entry, leaving the owning run's binding in place.

    A child turn that borrowed its parent's session releases through here. The
    owner's cascading release would take the parent's own entry with it, since
    both name the same binding object, and the parent run is still using it.
    """
    _authenticated_bindings.pop(subconversation_id, None)


def release_authenticated_session(subconversation_id: str) -> None:
    """Drop a binding and every memoized child of it."""
    binding = _authenticated_bindings.pop(subconversation_id, None)
    if binding is None:
        return
    for key, candidate in list(_authenticated_bindings.items()):
        if candidate is binding:
            del _authenticated_bindings[key]


def authenticated_binding_for(
    subconversation_id: str | None,
) -> AuthenticatedSessionBinding | None:
    """The binding registered directly against *subconversation_id*, if any."""
    if subconversation_id is None:
        return None
    return _authenticated_bindings.get(subconversation_id)


def authenticated_profile_ids(
    exec_context: ToolExecutionContext,
) -> frozenset[str]:
    """Profiles that may only ever operate inside an authenticated session.

    Read from the site configuration rather than from a hard-coded list, so an
    operator who points a site at their own profile gets the same fail-closed
    treatment as the shipped ones.
    """
    service = getattr(exec_context, "processing_service", None)
    app_config = getattr(service, "app_config", None) if service is not None else None
    sites = getattr(app_config, "authenticated_sites", None) if app_config else None
    if not sites:
        return frozenset()
    return frozenset(
        profile_id
        for site in sites.values()
        for profile_id in (site.browser_profile, site.visual_profile)
    )


def _requires_authenticated_binding(exec_context: ToolExecutionContext) -> bool:
    profile_id = getattr(exec_context, "processing_profile_id", None)
    return profile_id is not None and profile_id in authenticated_profile_ids(
        exec_context
    )


async def resolve_authenticated_binding(
    exec_context: ToolExecutionContext,
) -> AuthenticatedSessionBinding | None:
    """The authenticated session this turn is running inside, if any.

    Resolves the turn's own subconversation first. A turn that is the visual
    hop of an authenticated run has its own subconversation, so one parent link
    is followed through the delegation run record and then memoized; nothing
    deeper is walked, because ownership deliberately does not fan out past that
    hop.
    """
    # Checked first so the ordinary browsing path -- every conversation in a
    # deployment that configures no authenticated site -- costs one dict test
    # and never touches the context or the database. A turn that *must* have a
    # binding is not let through on this shortcut: its absence is the
    # fail-closed case, decided by the caller.
    if not _authenticated_bindings:
        return None
    subconversation_id = getattr(exec_context, "subconversation_id", None)
    if subconversation_id is None:
        return None
    binding = _authenticated_bindings.get(subconversation_id)
    if binding is not None:
        return binding
    run = await exec_context.db_context.delegation_runs.get_by_subconversation_id(
        subconversation_id
    )
    parent = run["source_subconversation_id"] if run is not None else None
    if parent is None:
        return None
    binding = _authenticated_bindings.get(parent)
    if binding is not None:
        _authenticated_bindings[subconversation_id] = binding
    return binding


def _remote_enabled(exec_context: ToolExecutionContext) -> BrowserHandoffConfig | None:
    """Return the browser-handoff config if the remote backend should be used here."""
    service = getattr(exec_context, "processing_service", None)
    app_config = getattr(service, "app_config", None) if service is not None else None
    if app_config is None:
        return None
    config = app_config.browser_handoff_config
    if not config.enabled or not config.service_url:
        return None
    profile_id = getattr(exec_context, "processing_profile_id", None)
    if (
        config.handoff_capable_profiles
        and profile_id not in config.handoff_capable_profiles
    ):
        return None
    return config


async def get_browser_backend(exec_context: ToolExecutionContext) -> BrowserBackend:
    """Resolve the browser backend for this execution context.

    Uses the remote ``browser-server`` backend when ``browser_handoff_config`` is
    enabled for the active profile (including ``browser_visual_profile``); otherwise
    the shared local Playwright session.  Both the semantic DOM profile and the
    visual Computer Use profile share the same remote session keyed by
    ``conversation_id``, so the tab state (URL, cookies, form fills) is preserved
    across profile delegation.
    """
    binding = await resolve_authenticated_binding(exec_context)
    if binding is not None:
        return binding.backend
    if _requires_authenticated_binding(exec_context):
        # An authenticated browser profile with no bound session has nothing it
        # is allowed to drive. Falling through would hand it the conversation's
        # ordinary backend, which would create a fresh *unconfined* session --
        # the exact escape the never-re-provision rule exists to prevent. It
        # happens for real after a restart, when a queued authenticated run
        # outlives the in-process binding, so it fails closed rather than
        # quietly widening.
        raise AuthenticatedSessionUnavailableError(
            "This profile only operates inside an authenticated-site session, "
            "and no session is bound to this run. The run cannot continue; "
            "start the task again."
        )
    config = _remote_enabled(exec_context)
    if config is not None:
        session_key = exec_context.conversation_id or "default"
        backend = _remote_backends.get(session_key)
        if backend is None:
            tz = getattr(exec_context, "timezone", None)
            timezone_id = str(tz) if tz else None
            backend = RemoteBrowserBackend(config, session_key, timezone_id=timezone_id)
            _remote_backends[session_key] = backend
        return backend
    session: BrowserSession = await get_browser_session(exec_context)
    return LocalPlaywrightBackend(session)


async def close_browser_backend(exec_context: ToolExecutionContext) -> None:
    """Close and remove any backend (local or remote) for this context."""
    session_key = exec_context.conversation_id or "default"
    remote = _remote_backends.pop(session_key, None)
    if remote is not None:
        await remote.close()
    await close_browser_session(exec_context)
