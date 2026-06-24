"""Unit tests for the semantic DOM browser tools.

These tests cover the pure-Python helpers in :mod:`family_assistant.tools.browser_dom`:
TOON rendering (via the ``toons`` library), query filtering, ref collection/resolution,
load-state coercion, and ref-cache invalidation. The Playwright-driven tool
implementations are exercised in :mod:`tests.functional.tools.test_browser_dom`.
"""

from __future__ import annotations

import socket
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
import toons

from family_assistant.services.ucp import MerchantUCPProfile
from family_assistant.tools import browser_dom
from family_assistant.tools.browser_backend import (
    LocalPlaywrightBackend,
    _coerce_load_state,  # noqa: PLC2701  # Testing private load-state narrowing helper
)
from family_assistant.tools.browser_dom import (
    Snapshot,
    SnapshotNode,
    _any_match,  # noqa: PLC2701  # Testing private query matcher used by renderer
    _collect_refs,  # noqa: PLC2701  # Testing private ref-tree walker
    _format_toon,  # noqa: PLC2701  # Testing private TOON renderer
    _format_ucp_hint,  # noqa: PLC2701  # Testing private UCP hint renderer
    _host_resolves_to_private,  # noqa: PLC2701  # Testing private SSRF guard
    _probe_ucp_support,  # noqa: PLC2701  # Testing private UCP probe + cache
    _resolve_ref,  # noqa: PLC2701  # Testing private ref-cache lookup
)
from family_assistant.tools.browser_session import BrowserSession

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext


def _link_node(ref: str, name: str, href: str) -> SnapshotNode:
    return {"ref": ref, "role": "link", "name": name, "href": href}


def _heading_node(ref: str, name: str) -> SnapshotNode:
    return {"ref": ref, "role": "heading", "name": name}


def _input_node(ref: str, name: str, value: str | None = None) -> SnapshotNode:
    node: SnapshotNode = {
        "ref": ref,
        "role": "textbox",
        "name": name,
        "tag": "input",
        "input_type": "text",
    }
    if value is not None:
        node["value"] = value
    return node


def _sample_snapshot() -> Snapshot:
    return {
        "url": "https://example.com/",
        "title": "Example",
        "forms": 1,
        "elements": 4,
        "roots": [
            _heading_node("e1", "Welcome"),
            {
                "ref": "e2",
                "role": "form",
                "name": "",
                "children": [
                    _input_node("e3", "Search", value="kittens"),
                    _link_node("e4", "About us", "https://example.com/about"),
                ],
            },
        ],
    }


class TestFormatToon:
    """TOON v3 rendering of accessibility snapshots."""

    def test_round_trips_via_the_toons_library(self) -> None:
        """The renderer must emit real TOON — ``toons.loads`` should recover
        the structure we asked ``toons.dumps`` to encode."""
        text = _format_toon(_sample_snapshot())
        parsed = toons.loads(text)
        assert parsed["url"] == "https://example.com/"
        assert parsed["title"] == "Example"
        assert parsed["forms"] == 1
        assert parsed["elements"] == 4
        assert len(parsed["roots"]) == 2

    def test_renders_scalar_header_fields(self) -> None:
        text = _format_toon(_sample_snapshot())
        assert "url: " in text and "https://example.com/" in text
        assert "title: Example" in text
        assert "forms: 1" in text
        assert "elements: 4" in text

    def test_renders_refs_and_roles_for_each_node(self) -> None:
        text = _format_toon(_sample_snapshot())
        # TOON renders nested dicts with `ref:`/`role:` keys — the LLM finds
        # nodes by these tokens rather than a synthetic `[eN]` prefix.
        assert "ref: e1" in text
        assert "role: heading" in text
        assert "ref: e4" in text
        assert "role: link" in text

    def test_renders_input_attributes_on_their_own_lines(self) -> None:
        text = _format_toon(_sample_snapshot())
        assert "ref: e3" in text
        assert "input_type: text" in text
        assert "value: kittens" in text

    def test_renders_link_href(self) -> None:
        text = _format_toon(_sample_snapshot())
        assert "About us" in text
        assert "example.com/about" in text

    def test_query_filters_out_nonmatching_branches(self) -> None:
        text = _format_toon(_sample_snapshot(), query="about")
        parsed = toons.loads(text)
        # Heading "Welcome" doesn't match "about", so only the form survives.
        roots = parsed["roots"]
        assert len(roots) == 1
        assert roots[0]["role"] == "form"

    def test_query_keeps_ancestor_of_matching_descendant(self) -> None:
        text = _format_toon(_sample_snapshot(), query="about")
        parsed = toons.loads(text)
        # The form parent of e4 should survive the filter because its subtree
        # contains a matching link — otherwise refs would be orphaned.
        form = parsed["roots"][0]
        assert form["ref"] == "e2"
        link = form["children"][0]
        assert link["ref"] == "e4"
        assert link["role"] == "link"

    def test_query_with_no_matches_adds_placeholder_note(self) -> None:
        text = _format_toon(_sample_snapshot(), query="nonexistent")
        assert "no matches for query=" in text


class TestAnyMatch:
    """Deep query matching used by the tree filter to keep ancestors."""

    def test_matches_by_role(self) -> None:
        nodes = [_heading_node("e1", "Hello")]
        assert _any_match(nodes, "heading") is True

    def test_matches_by_href(self) -> None:
        nodes = [_link_node("e1", "Other", "https://foo.example")]
        assert _any_match(nodes, "foo.example") is True

    def test_recurses_into_children(self) -> None:
        parent: SnapshotNode = {
            "ref": "e1",
            "role": "form",
            "name": "",
            "children": [_link_node("e2", "Deep", "https://x/")],
        }
        assert _any_match([parent], "deep") is True

    def test_no_query_matches_everything(self) -> None:
        assert _any_match([_heading_node("e1", "x")], None) is True


class TestCollectRefs:
    """Collecting refs from a snapshot tree for the session cache."""

    def test_returns_selector_for_every_node(self) -> None:
        refs = _collect_refs(_sample_snapshot()["roots"])
        assert refs == {
            "e1": '[data-fa-ref="e1"]',
            "e2": '[data-fa-ref="e2"]',
            "e3": '[data-fa-ref="e3"]',
            "e4": '[data-fa-ref="e4"]',
        }

    def test_handles_empty_children_list(self) -> None:
        node: SnapshotNode = {"ref": "e1", "role": "text", "name": "", "children": []}
        assert _collect_refs([node]) == {"e1": '[data-fa-ref="e1"]'}


class TestResolveRef:
    """Ref resolution against the session cache."""

    def test_returns_selector_when_ref_is_known(self) -> None:
        backend = LocalPlaywrightBackend(BrowserSession())
        backend.ref_cache["e7"] = '[data-fa-ref="e7"]'
        assert _resolve_ref(backend, "e7") == '[data-fa-ref="e7"]'

    def test_raises_valueerror_with_known_refs_listed(self) -> None:
        backend = LocalPlaywrightBackend(BrowserSession())
        backend.ref_cache["e1"] = '[data-fa-ref="e1"]'
        backend.ref_cache["e2"] = '[data-fa-ref="e2"]'
        with pytest.raises(ValueError, match="Unknown ref 'e99'") as exc:
            _resolve_ref(backend, "e99")
        assert "e1" in str(exc.value)


class TestCoerceLoadState:
    """Runtime narrowing of load-state strings to the Literal type."""

    @pytest.mark.parametrize("state", ["load", "domcontentloaded", "networkidle"])
    def test_accepts_valid_states(self, state: str) -> None:
        assert _coerce_load_state(state) == state

    def test_rejects_unknown_state(self) -> None:
        with pytest.raises(ValueError, match="Invalid load state"):
            _coerce_load_state("commit")


class TestBrowserSessionRefCache:
    """Ref-cache invalidation contract.

    Navigation (or arbitrary in-page JS via ``browser_exec``) can stale refs;
    the session is responsible for clearing them so the next snapshot starts
    clean.
    """

    def test_clear_refs_empties_the_cache(self) -> None:
        session = BrowserSession()
        session.ref_cache.update({
            "e1": '[data-fa-ref="e1"]',
            "e2": '[data-fa-ref="e2"]',
        })
        session.clear_refs()
        assert session.ref_cache == {}

    def test_clear_refs_is_idempotent(self) -> None:
        session = BrowserSession()
        session.clear_refs()
        session.clear_refs()
        assert session.ref_cache == {}


def _shopping_profile(origin: str) -> MerchantUCPProfile:
    return MerchantUCPProfile(
        origin=origin,
        mcp_endpoint=f"{origin}/api/ucp/mcp",
        service_names=("dev.ucp.shopping",),
        capability_names=("dev.ucp.shopping.cart", "dev.ucp.shopping.checkout"),
        version="2026-04-08",
    )


class TestFormatUcpHint:
    """The hint line appended to a snapshot when the site supports UCP."""

    def test_includes_origin_business_url_and_capabilities(self) -> None:
        hint = _format_ucp_hint(_shopping_profile("https://shop.example.com"))
        assert "https://shop.example.com" in hint
        assert 'business_url="https://shop.example.com"' in hint
        assert "cart, checkout" in hint
        assert "ucp_add_to_cart" in hint

    def test_omits_capability_list_when_none_advertised(self) -> None:
        profile = MerchantUCPProfile(
            origin="https://shop.example.com",
            mcp_endpoint="https://shop.example.com/api/ucp/mcp",
            service_names=("dev.ucp.shopping",),
            capability_names=(),
            version=None,
        )
        hint = _format_ucp_hint(profile)
        assert "Capabilities:" not in hint


class TestHostResolvesToPrivate:
    """SSRF guard: only globally-routable hosts may be probed."""

    def test_blocks_loopback_literal(self) -> None:
        assert _host_resolves_to_private("127.0.0.1") is True

    @pytest.mark.parametrize("host", ["10.0.0.5", "192.168.1.1", "169.254.0.1"])
    def test_blocks_private_and_link_local_literals(self, host: str) -> None:
        assert _host_resolves_to_private(host) is True

    def test_allows_public_literal(self) -> None:
        assert _host_resolves_to_private("8.8.8.8") is False

    def test_blocks_unresolvable_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_args: object, **_kwargs: object) -> object:
            raise socket.gaierror("name or service not known")

        monkeypatch.setattr(browser_dom.socket, "getaddrinfo", boom)
        assert _host_resolves_to_private("nonexistent.invalid") is True


class TestProbeUcpSupport:
    """Per-session caching probe used by snapshot-returning browser tools."""

    def _context(self, conversation_id: str) -> ToolExecutionContext:
        return cast(
            "ToolExecutionContext", SimpleNamespace(conversation_id=conversation_id)
        )

    def _allow_all_origins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def allow(_origin: str) -> bool:
            return False

        monkeypatch.setattr(browser_dom, "_origin_is_blocked", allow)

    async def test_returns_hint_and_caches_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._allow_all_origins(monkeypatch)
        calls: list[str] = []

        async def fake_discover(
            url: str, *, client: object
        ) -> MerchantUCPProfile | None:
            calls.append(url)
            return _shopping_profile("https://shop.example.com")

        monkeypatch.setattr(browser_dom, "discover_merchant_ucp_profile", fake_discover)
        context = self._context("probe-cache-test")

        first = await _probe_ucp_support(context, "https://shop.example.com/products/x")
        second = await _probe_ucp_support(context, "https://shop.example.com/cart")

        assert first is not None
        assert "ucp_add_to_cart" in first
        assert second == first
        # Discovery runs once per origin; the second navigation hits the cache.
        assert len(calls) == 1

    async def test_returns_none_for_non_https_origin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fail_discover(
            url: str, *, client: object
        ) -> MerchantUCPProfile | None:  # pragma: no cover - must not be called
            raise AssertionError("non-HTTPS origin must not be probed")

        monkeypatch.setattr(browser_dom, "discover_merchant_ucp_profile", fail_discover)
        result = await _probe_ucp_support(
            self._context("probe-http-test"), "http://shop.example.com"
        )
        assert result is None

    async def test_skips_probe_for_private_origin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fail_discover(
            url: str, *, client: object
        ) -> MerchantUCPProfile | None:  # pragma: no cover - must not be called
            raise AssertionError("private origin must not be probed")

        monkeypatch.setattr(browser_dom, "discover_merchant_ucp_profile", fail_discover)
        result = await _probe_ucp_support(
            self._context("probe-private-test"), "https://10.0.0.5/products/x"
        )
        assert result is None

    async def test_caches_negative_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._allow_all_origins(monkeypatch)
        calls: list[str] = []

        async def fake_discover(
            url: str, *, client: object
        ) -> MerchantUCPProfile | None:
            calls.append(url)
            return None

        monkeypatch.setattr(browser_dom, "discover_merchant_ucp_profile", fake_discover)
        context = self._context("probe-negative-test")

        first = await _probe_ucp_support(context, "https://plain.example.com/")
        second = await _probe_ucp_support(context, "https://plain.example.com/page")

        assert first is None
        assert second is None
        assert len(calls) == 1
