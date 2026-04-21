"""Unit tests for the semantic DOM browser tools.

These tests cover the pure-Python helpers in :mod:`family_assistant.tools.browser_dom`:
TOON rendering, query filtering, ref collection/resolution, load-state coercion,
and ref-cache invalidation. The Playwright-driven tool implementations are
exercised in :mod:`tests.functional.tools.test_browser_dom`.
"""

from __future__ import annotations

import pytest

from family_assistant.tools.browser_dom import (
    Snapshot,
    SnapshotNode,
    _any_match,  # noqa: PLC2701  # Testing private query matcher used by renderer
    _coerce_load_state,  # noqa: PLC2701  # Testing private load-state narrowing helper
    _collect_refs,  # noqa: PLC2701  # Testing private ref-tree walker
    _format_toon,  # noqa: PLC2701  # Testing private TOON renderer
    _resolve_ref,  # noqa: PLC2701  # Testing private ref-cache lookup
)
from family_assistant.tools.browser_session import BrowserSession


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
    """TOON-style rendering of accessibility snapshots."""

    def test_header_lines_include_url_title_and_counts(self) -> None:
        text = _format_toon(_sample_snapshot())
        # The header block should be stable and cheap to diff against.
        header = text.splitlines()[:5]
        assert header == [
            "page:",
            "  url: https://example.com/",
            "  title: Example",
            "  forms: 1",
            "  elements: 4",
        ]

    def test_renders_refs_with_roles_and_names(self) -> None:
        text = _format_toon(_sample_snapshot())
        assert '[e1] heading "Welcome"' in text
        assert '[e4] link "About us" href=https://example.com/about' in text

    def test_includes_input_value_and_type_as_attributes(self) -> None:
        text = _format_toon(_sample_snapshot())
        # Input values are rendered via repr() so quotes are preserved.
        assert "[e3] textbox" in text
        assert "type=text" in text
        assert "value='kittens'" in text

    def test_nodes_without_a_name_omit_the_label_quotes(self) -> None:
        snap: Snapshot = {
            "url": "u",
            "title": "t",
            "forms": 0,
            "elements": 1,
            "roots": [{"ref": "e1", "role": "form", "name": ""}],
        }
        text = _format_toon(snap)
        # Empty-name nodes shouldn't emit bare `""` — the role stands alone.
        assert "[e1] form\n" in text

    def test_query_filters_out_nonmatching_branches(self) -> None:
        text = _format_toon(_sample_snapshot(), query="about")
        assert "[e4] link" in text
        # Heading "Welcome" doesn't match "about", and has no matching descendants.
        assert '[e1] heading "Welcome"' not in text

    def test_query_keeps_ancestor_of_matching_descendant(self) -> None:
        text = _format_toon(_sample_snapshot(), query="about")
        # The form parent of e4 should survive the filter because its subtree
        # contains a matching link — otherwise refs would be orphaned.
        assert "[e2] form" in text

    def test_query_with_no_matches_adds_placeholder(self) -> None:
        text = _format_toon(_sample_snapshot(), query="nonexistent")
        assert "no matches for query=" in text


class TestAnyMatch:
    """Deep query matching used by the TOON renderer to keep ancestors."""

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
            "e1": '[data-ref="e1"]',
            "e2": '[data-ref="e2"]',
            "e3": '[data-ref="e3"]',
            "e4": '[data-ref="e4"]',
        }

    def test_handles_empty_children_list(self) -> None:
        node: SnapshotNode = {"ref": "e1", "role": "text", "name": "", "children": []}
        assert _collect_refs([node]) == {"e1": '[data-ref="e1"]'}


class TestResolveRef:
    """Ref resolution against the session cache."""

    def test_returns_selector_when_ref_is_known(self) -> None:
        session = BrowserSession()
        session.ref_cache["e7"] = '[data-ref="e7"]'
        assert _resolve_ref(session, "e7") == '[data-ref="e7"]'

    def test_raises_valueerror_with_known_refs_listed(self) -> None:
        session = BrowserSession()
        session.ref_cache["e1"] = '[data-ref="e1"]'
        session.ref_cache["e2"] = '[data-ref="e2"]'
        with pytest.raises(ValueError, match="Unknown ref 'e99'") as exc:
            _resolve_ref(session, "e99")
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
        session.ref_cache.update({"e1": '[data-ref="e1"]', "e2": '[data-ref="e2"]'})
        session.clear_refs()
        assert session.ref_cache == {}

    def test_clear_refs_is_idempotent(self) -> None:
        session = BrowserSession()
        session.clear_refs()
        session.clear_refs()
        assert session.ref_cache == {}
