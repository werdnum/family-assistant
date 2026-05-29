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
    from rebrowser_playwright.async_api import Page

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

# In-page DOM walker shared by the local backend. Tags interactive/labeled
# elements with a stable ``data-fa-ref`` attribute and returns a nested
# accessibility tree matching the ``Snapshot`` shape. ``browser-server`` runs an
# identical copy server-side, so the remote backend returns the same structure.
# The ref ``e12`` always resolves to ``[data-fa-ref="e12"]``.
SNAPSHOT_JS = r"""
() => {
  document.querySelectorAll('[data-fa-ref]').forEach(el => el.removeAttribute('data-fa-ref'));

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

  function walk(el, out) {
    if (el.nodeType !== 1) return;
    if (!isVisible(el)) return;
    const role = interesting(el);
    if (role) {
      const ref = allocRef();
      el.setAttribute('data-fa-ref', ref);
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


class HandoffUnavailableError(BrowserBackendError):
    """Raised when a human handoff is requested but no remote backend is active."""


@runtime_checkable
class BrowserBackend(Protocol):
    """Page-level operations the semantic DOM tools depend on.

    ``ref_cache`` maps short refs (``e12``) to selectors; it is repopulated on
    each snapshot and cleared on navigation / arbitrary JS execution.
    """

    @property
    def ref_cache(self) -> dict[str, str]: ...

    @property
    def current_url(self) -> str: ...

    def clear_refs(self) -> None: ...

    async def goto(self, url: str) -> None: ...

    async def raw_snapshot(self) -> Snapshot: ...

    async def settle(self, timeout_ms: int = 5000) -> None: ...

    async def click(self, selector: str) -> None: ...

    async def fill(self, selector: str, text: str, submit: bool) -> None: ...

    async def select(self, selector: str, value: str) -> None: ...

    async def wait(self, selector: str | None, state: str, timeout_ms: int) -> None: ...

    async def extract_html(self, selector: str | None) -> str: ...

    async def screenshot_png(self) -> bytes: ...

    # ast-grep-ignore: no-dict-any - JS return values are genuinely arbitrary JSON
    async def evaluate(self, code: str) -> Any: ...  # noqa: ANN401

    async def request_handoff(
        self,
        *,
        reason: str,
        handoff_note: str,
        expected_origin: str | None,
        allowed_resume: str,
    ) -> JsonDict: ...

    async def close(self) -> None: ...


class LocalPlaywrightBackend:
    """Backend wrapping the shared in-process Playwright ``BrowserSession``."""

    def __init__(self, session: BrowserSession) -> None:
        self._session = session

    @property
    def ref_cache(self) -> dict[str, str]:
        return self._session.ref_cache

    @property
    def current_url(self) -> str:
        page = self._session.page
        return page.url if page is not None else ""

    def clear_refs(self) -> None:
        self._session.clear_refs()

    async def _page(self) -> Page:
        return await self._session.ensure_page()

    async def goto(self, url: str) -> None:
        page = await self._page()
        await page.goto(url)

    async def raw_snapshot(self) -> Snapshot:
        page = await self._page()
        raw = await page.evaluate(SNAPSHOT_JS)
        return cast("Snapshot", raw)

    async def settle(self, timeout_ms: int = 5000) -> None:
        page = await self._page()
        with contextlib.suppress(PlaywrightError):
            await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)

    async def click(self, selector: str) -> None:
        page = await self._page()
        await page.locator(selector).click()

    async def fill(self, selector: str, text: str, submit: bool) -> None:
        page = await self._page()
        locator = page.locator(selector)
        await locator.fill(text)
        if submit:
            await locator.press("Enter")

    async def select(self, selector: str, value: str) -> None:
        page = await self._page()
        await page.locator(selector).select_option(value)

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

    async def request_handoff(
        self,
        *,
        reason: str,
        handoff_note: str,
        expected_origin: str | None,
        allowed_resume: str,
    ) -> JsonDict:
        raise HandoffUnavailableError(
            "Human browser handoff requires browser_handoff_config to be enabled "
            "(no remote browser-server is configured)."
        )

    async def close(self) -> None:
        await self._session.close()


class RemoteBrowserBackend:
    """Backend that drives a remote ``browser-server`` session over HTTP."""

    def __init__(
        self,
        config: BrowserHandoffConfig,
        conversation_id: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not config.service_url:
            raise BrowserBackendError(
                "browser_handoff_config.enabled is set but service_url is missing"
            )
        self._config = config
        self._conversation_id = conversation_id
        self._base_url = config.service_url.rstrip("/")
        self._session_id: str | None = None
        # ``client`` is an injection seam for tests (e.g. httpx.MockTransport).
        self._client = client or httpx.AsyncClient(timeout=config.timeout_seconds)
        self.ref_cache: dict[str, str] = {}
        self._last_url: str = ""

    @property
    def current_url(self) -> str:
        return self._last_url

    def clear_refs(self) -> None:
        self.ref_cache.clear()

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

    async def _ensure_session(self) -> str:
        if self._session_id is not None:
            return self._session_id
        resp = await self._client.post(
            f"{self._base_url}/v1/sessions",
            headers=self._headers(),
            json={
                "conversation_id": self._conversation_id,
                "interface_type": "research",
                "initial_owner": "agent",
            },
        )
        self._raise_for_status(resp, "create session")
        self._session_id = str(resp.json()["session_id"])
        return self._session_id

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

    async def raw_snapshot(self) -> Snapshot:
        result = await self._command("snapshot")
        return cast("Snapshot", result)

    async def settle(self, timeout_ms: int = 5000) -> None:
        # browser-server navigates with wait_until=domcontentloaded and settles
        # after click/type itself, so there is nothing extra to wait for here.
        return None

    async def click(self, selector: str) -> None:
        await self._command("click", {"selector": selector})

    async def fill(self, selector: str, text: str, submit: bool) -> None:
        await self._command("type_text", {"selector": selector, "text": text})
        if submit:
            await self._command("press_key", {"key": "Enter"})

    async def select(self, selector: str, value: str) -> None:
        await self._command("select", {"selector": selector, "value": value})

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
        result = await self._command("exec", {"code": code})
        if "error" in result:
            raise BrowserBackendError(str(result["error"]))
        return result.get("result")

    async def request_handoff(
        self,
        *,
        reason: str,
        handoff_note: str,
        expected_origin: str | None,
        allowed_resume: str,
    ) -> JsonDict:
        session_id = await self._ensure_session()
        payload: JsonDict = {
            "reason": reason,
            "handoff_note": handoff_note,
            "allowed_resume": allowed_resume,
        }
        if expected_origin is not None:
            payload["expected_origin"] = expected_origin
        resp = await self._client.post(
            f"{self._base_url}/v1/sessions/{session_id}/handoff",
            headers=self._headers(),
            json=payload,
        )
        self._raise_for_status(resp, "handoff")
        return resp.json()

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


# Remote backends are keyed by conversation_id, mirroring the local
# BrowserSession registry so each conversation drives its own remote session.
_remote_backends: dict[str, RemoteBrowserBackend] = {}


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
    enabled for the active profile; otherwise the shared local Playwright session.
    """
    config = _remote_enabled(exec_context)
    if config is not None:
        session_key = exec_context.conversation_id or "default"
        backend = _remote_backends.get(session_key)
        if backend is None:
            backend = RemoteBrowserBackend(config, session_key)
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
