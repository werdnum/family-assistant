"""History taint lasts as long as the row that introduced it is in the prompt."""

from __future__ import annotations

from types import SimpleNamespace

from family_assistant.security.taint import (
    DEFAULT_MAX_SEEN_KEYS,
    DEFAULT_MAX_SOURCES,
    LEGACY_MISSING_TAINT_METADATA_LABEL,
    PRE_ORIGIN_TAINT_METADATA_VERSION,
    TAINT_METADATA_VERSION,
    InMemoryTurnTaintTracker,
    SourceTrustTier,
    TaintMetadata,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
    is_human_direct_metadata,
    merge_history_taint,
    merge_taint_state_into_tracker,
    prompt_window_taint,
    raise_taint_state_to,
    sources_explaining_state,
    strip_legacy_labeled_echoes,
)


def _source(
    tier: SourceTrustTier, source_id: str, *, inherited: bool = False
) -> TaintSource:
    return TaintSource(
        source_type=TaintSourceType.TOOL_OUTPUT,
        source_id=source_id,
        tier=tier,
        labels=frozenset(),
        reason=f"{source_id} output",
        inherited=inherited,
    )


def _row(metadata: TaintMetadata | None) -> SimpleNamespace:
    return SimpleNamespace(taint_metadata=metadata)


def _user_typed() -> TaintSource:
    return TaintSource(
        source_type=TaintSourceType.USER_MESSAGE,
        source_id="user",
        tier=SourceTrustTier.TRUSTED_USER,
        labels=frozenset(),
        reason="Typed by the user.",
    )


def _web_search_row() -> TaintMetadata:
    """A reply written by a turn that searched the web itself."""
    return (
        TurnTaintState
        .empty()
        .add_source(_user_typed())
        .add_source(_source(SourceTrustTier.UNKNOWN_EXTERNAL, "web_search"))
        .with_authorship_floor()
        .to_metadata()
    )


def _next_turn_row(window: list[SimpleNamespace]) -> TaintMetadata:
    """A reply written by a turn that read nothing new beyond its window."""
    return (
        prompt_window_taint(window)
        .add_source(_user_typed())
        .with_authorship_floor()
        .to_metadata()
    )


def test_row_that_only_inherited_taint_does_not_pass_it_on() -> None:
    search_row = _row(_web_search_row())
    follow_up = _next_turn_row([search_row])

    assert follow_up.get("max_tier") == "unknown_external"
    assert follow_up.get("introduced_max_tier") == "trusted_internal"
    inherited = [s for s in follow_up.get("sources", []) if s.get("inherited")]
    assert [s["source_id"] for s in inherited] == ["web_search"]

    assert (
        prompt_window_taint([search_row, _row(follow_up)]).max_tier
        is SourceTrustTier.UNKNOWN_EXTERNAL
    )
    healed = prompt_window_taint([_row(follow_up)])
    assert healed.max_tier is SourceTrustTier.TRUSTED_INTERNAL
    assert not healed.history_high_taint_present


def test_window_carry_in_is_inherited_and_merged_into_the_turn() -> None:
    state = prompt_window_taint([_row(_web_search_row())])

    assert state.max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert state.introduced_max_tier is SourceTrustTier.TRUSTED_USER
    assert state.history_high_taint_present
    assert all(source.inherited for source in state.sources)


def test_reintroducing_an_inherited_source_makes_it_the_turns_own() -> None:
    state = prompt_window_taint([_row(_web_search_row())])
    reread = state.add_source(_source(SourceTrustTier.UNKNOWN_EXTERNAL, "web_search"))

    assert reread.introduced_max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    (web,) = [s for s in reread.sources if s.source_id == "web_search"]
    assert not web.inherited
    assert reread.sources[-1] == web
    assert reread.distinct_source_count == state.distinct_source_count


def _crowded_by_inherited_untrusted(introduced: TaintSource) -> TurnTaintState:
    """A turn whose own source the bound evicts behind untrusted carry-in."""
    state = TurnTaintState.empty().add_source(introduced)
    for index in range(DEFAULT_MAX_SOURCES):
        state = state.add_source(
            _source(SourceTrustTier.UNKNOWN_EXTERNAL, f"w{index}", inherited=True),
            from_history=True,
        )
    assert introduced not in state.sources
    return state


def test_introduced_maximum_survives_eviction_from_the_source_bound() -> None:
    state = _crowded_by_inherited_untrusted(
        _source(SourceTrustTier.RECOGNIZED_MACHINE, "fetch_feed")
    )
    metadata = state.to_metadata()

    assert metadata.get("introduced_max_tier") == "recognized_machine"
    assert (
        prompt_window_taint([_row(metadata)]).max_tier
        is SourceTrustTier.RECOGNIZED_MACHINE
    )


def test_source_bound_evicts_clean_sources_before_the_untrusted_one() -> None:
    state = TurnTaintState.empty().add_source(
        _source(SourceTrustTier.UNKNOWN_EXTERNAL, "web_search")
    )
    for index in range(DEFAULT_MAX_SOURCES + 2):
        state = state.add_source(_source(SourceTrustTier.TRUSTED_INTERNAL, f"t{index}"))

    assert len(state.sources) == DEFAULT_MAX_SOURCES
    assert state.sources[0].source_id == "web_search"
    assert [s.source_id for s in state.sources[1:]] == [
        f"t{index}" for index in range(3, DEFAULT_MAX_SOURCES + 2)
    ]
    metadata = state.to_metadata()
    assert "web_search" in [s["source_id"] for s in metadata.get("sources", [])]


def test_retained_source_is_deduplicated_after_its_index_key_ages_out() -> None:
    web = _source(SourceTrustTier.UNKNOWN_EXTERNAL, "web_search")
    state = TurnTaintState.empty().add_source(web)
    for index in range(DEFAULT_MAX_SEEN_KEYS + 1):
        state = state.add_source(_source(SourceTrustTier.TRUSTED_INTERNAL, f"t{index}"))
    distinct = state.distinct_source_count

    state = state.add_source(web)

    assert [s.source_id for s in state.sources].count("web_search") == 1
    assert state.distinct_source_count == distinct


def test_legacy_row_contributes_attributed_sources_not_its_merged_maximum() -> None:
    legacy: TaintMetadata = {
        "version": PRE_ORIGIN_TAINT_METADATA_VERSION,
        "max_tier": "unknown_external",
        "history_high_taint_present": True,
        "fresh_high_taint_seen_at_sequence": None,
        "sources": [
            {
                "source_type": "tool_output",
                "source_id": "web_search",
                "tier": "recognized_machine",
                "labels": [],
                "reason": "web_search output",
            },
            {
                "source_type": "manual",
                "source_id": None,
                "tier": "unknown_external",
                "labels": [],
                "reason": "Merged taint state max_tier exceeded retained summaries.",
            },
        ],
        "approved_sinks": [],
    }

    state = prompt_window_taint([_row(legacy)])
    assert state.max_tier is SourceTrustTier.RECOGNIZED_MACHINE
    assert all(source.inherited for source in state.sources)

    follow_up = _next_turn_row([_row(legacy)])
    assert follow_up.get("introduced_max_tier") == "trusted_internal"
    assert (
        prompt_window_taint([_row(follow_up)]).max_tier
        is SourceTrustTier.TRUSTED_INTERNAL
    )


def test_legacy_row_whose_capped_sources_dropped_its_tier_still_taints() -> None:
    """A runtime_v2 row evicted oldest-first, so its untrusted source may be gone."""
    capped: TaintMetadata = {
        "version": PRE_ORIGIN_TAINT_METADATA_VERSION,
        "max_tier": "unknown_external",
        "history_high_taint_present": False,
        "fresh_high_taint_seen_at_sequence": 1,
        "sources": [
            {
                "source_type": "tool_output",
                "source_id": f"t{index}",
                "tier": "trusted_internal",
                "labels": [],
                "reason": f"t{index} output",
            }
            for index in range(DEFAULT_MAX_SOURCES)
        ],
        "approved_sinks": [],
        "total_source_count": DEFAULT_MAX_SOURCES + 1,
        "distinct_source_count": DEFAULT_MAX_SOURCES + 1,
        "omitted_source_count": 1,
    }

    state = prompt_window_taint([_row(capped)])
    assert state.max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert state.history_high_taint_present

    follow_up = _next_turn_row([_row(capped)])
    assert follow_up.get("max_tier") == "unknown_external"


def test_legacy_row_with_unreadable_tier_stays_conservative() -> None:
    malformed: TaintMetadata = {
        "version": PRE_ORIGIN_TAINT_METADATA_VERSION,
        "max_tier": "not-a-tier",
    }
    assert (
        prompt_window_taint([_row(malformed)]).max_tier
        is SourceTrustTier.UNKNOWN_EXTERNAL
    )


def test_stored_stamp_read_by_default_is_introduced_by_the_reader() -> None:
    follow_up = _next_turn_row([_row(_web_search_row())])

    read_back = TurnTaintState.from_metadata(follow_up)

    assert read_back.introduced_max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert not any(source.inherited for source in read_back.sources)


def test_delegation_handoff_keeps_the_parents_carry_in_inherited() -> None:
    parent = prompt_window_taint([_row(_web_search_row())]).add_source(_user_typed())
    child_start = TurnTaintState.from_metadata(
        parent.to_metadata(), preserve_origin=True
    )
    child_result = child_start.add_source(
        _source(SourceTrustTier.RECOGNIZED_MACHINE, "child_fetch")
    )

    tracker = InMemoryTurnTaintTracker(parent)
    merged = merge_taint_state_into_tracker(
        tracker,
        TurnTaintState.from_metadata(child_result.to_metadata(), preserve_origin=True),
    )

    assert merged.max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert merged.introduced_max_tier is SourceTrustTier.RECOGNIZED_MACHINE
    (web,) = [s for s in merged.sources if s.source_id == "web_search"]
    assert web.inherited


def test_full_merge_of_a_turns_rows_keeps_origin_when_asked() -> None:
    follow_up = _next_turn_row([_row(_web_search_row())])

    same_turn = merge_history_taint([_row(follow_up)], preserve_origin=True)
    assert same_turn.max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert same_turn.introduced_max_tier is SourceTrustTier.TRUSTED_INTERNAL

    as_read = merge_history_taint([_row(follow_up)])
    assert as_read.introduced_max_tier is SourceTrustTier.UNKNOWN_EXTERNAL


def test_raise_is_measured_against_what_the_turn_introduced() -> None:
    state = prompt_window_taint([_row(_web_search_row())])

    raised = raise_taint_state_to(
        state, SourceTrustTier.RECOGNIZED_MACHINE, reason="Read a feed."
    )

    assert raised.introduced_max_tier is SourceTrustTier.RECOGNIZED_MACHINE
    assert raised.max_tier is SourceTrustTier.UNKNOWN_EXTERNAL


def test_human_direct_accepts_every_post_split_version() -> None:
    for version in (PRE_ORIGIN_TAINT_METADATA_VERSION, TAINT_METADATA_VERSION):
        assert is_human_direct_metadata({
            "version": version,
            "max_tier": "trusted_user",
        })
    assert not is_human_direct_metadata({
        "version": "runtime_v1",
        "max_tier": "trusted_user",
    })


def test_stamp_origin_round_trips_when_preserved() -> None:
    follow_up = _next_turn_row([_row(_web_search_row())])

    restored = TurnTaintState.from_metadata(
        follow_up, preserve_origin=True
    ).to_metadata()

    assert [s for s in restored.get("sources", []) if s.get("inherited")] == [
        s for s in follow_up.get("sources", []) if s.get("inherited")
    ]
    assert restored.get("max_tier") == follow_up.get("max_tier")
    assert restored.get("introduced_max_tier") == follow_up.get("introduced_max_tier")


def test_unrecognised_version_contributes_its_whole_stamp() -> None:
    future: TaintMetadata = {
        "version": "runtime_v99",
        "max_tier": "unknown_external",
        "sources": [],
    }

    assert (
        prompt_window_taint([_row(future)]).max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    )


def test_confirmed_continuation_keeps_the_turns_carry_in_inherited() -> None:
    turn = prompt_window_taint([_row(_web_search_row())]).add_source(_user_typed())
    tracker = InMemoryTurnTaintTracker(turn)

    merged = merge_taint_state_into_tracker(
        tracker,
        TurnTaintState.from_metadata(turn.to_metadata(), preserve_origin=True),
    )

    assert merged.introduced_max_tier is SourceTrustTier.TRUSTED_USER


def test_evicted_carry_in_is_not_promoted_to_introduced() -> None:
    state = prompt_window_taint([_row(_web_search_row())])
    for index in range(DEFAULT_MAX_SOURCES + 2):
        state = state.add_source(_source(SourceTrustTier.UNKNOWN_EXTERNAL, f"u{index}"))
    assert not any(source.inherited for source in state.sources)

    assert state.max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert state.history_high_taint_present


def test_stripping_an_inherited_echo_keeps_the_rows_introduced_maximum() -> None:
    echo = TaintSource(
        source_type=TaintSourceType.MANUAL,
        source_id=None,
        tier=SourceTrustTier.UNKNOWN_EXTERNAL,
        labels=frozenset({LEGACY_MISSING_TAINT_METADATA_LABEL}),
        reason="Legacy fallback echo.",
        inherited=True,
    )
    row = (
        TurnTaintState
        .empty()
        .add_source(echo, from_history=True)
        .add_source(_user_typed())
        .to_metadata()
    )

    stripped = strip_legacy_labeled_echoes(row)

    assert stripped is not None
    assert stripped.get("introduced_max_tier") == "trusted_user"


def test_replayed_sources_keep_a_maximum_whose_source_was_evicted() -> None:
    state = _crowded_by_inherited_untrusted(
        _source(SourceTrustTier.RECOGNIZED_MACHINE, "fetch_feed")
    )

    replayed = TurnTaintState.empty()
    for source in sources_explaining_state(state, reason="Replayed."):
        replayed = replayed.add_source(source)

    assert replayed.introduced_max_tier is SourceTrustTier.RECOGNIZED_MACHINE
