"""Tests for definition records on executable definitions (M1)."""

from __future__ import annotations

import json

import pytest

from family_assistant.security.definition_records import (
    CreationDisposition,
    DefinitionGateOutcome,
    DefinitionRecord,
    GateLayer,
    GateProvenance,
    authoring_taint_state,
    definition_content_hash,
    definition_record_from_row,
    stamp_definition,
)
from family_assistant.security.taint import (
    InMemoryTurnTaintTracker,
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)


def _tainted_state() -> TurnTaintState:
    return TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.EMAIL,
            source_id="msg-1",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset(),
            reason="Summarize this email.",
        )
    )


def _gate_outcome(
    disposition: CreationDisposition,
    *,
    mode: str = "observe",
    cure_permitted: bool = True,
) -> DefinitionGateOutcome:
    return DefinitionGateOutcome(
        disposition=disposition,
        gate=GateProvenance(layer=GateLayer.TAINT_CELL, mode=mode),
        cure_permitted=cure_permitted,
    )


def test_content_hash_is_stable_across_key_order() -> None:
    assert definition_content_hash({"a": 1, "b": [2, 3]}) == definition_content_hash({
        "b": [2, 3],
        "a": 1,
    })


def test_content_hash_changes_with_content() -> None:
    assert definition_content_hash({"code": "x"}) != definition_content_hash({
        "code": "y"
    })


def test_web_ui_writes_stamp_trusted_user() -> None:
    record = stamp_definition(
        content={"code": "x"},
        taint_state=TurnTaintState.empty(),
        human_direct=True,
    )

    assert record.taint_metadata.get("max_tier") == "trusted_user"
    assert record.disposition is CreationDisposition.CLEAN


def test_model_composed_clean_turn_writes_stamp_trusted_internal() -> None:
    record = stamp_definition(content={"code": "x"}, taint_state=TurnTaintState.empty())

    assert record.taint_metadata.get("max_tier") == "trusted_internal"


def test_mixed_turn_stamps_the_turn_maximum() -> None:
    record = stamp_definition(content={"code": "x"}, taint_state=_tainted_state())

    assert record.taint_metadata.get("max_tier") == "unknown_external"


def test_record_round_trips_through_storage() -> None:
    record = stamp_definition(
        content={"code": "x"},
        taint_state=_tainted_state(),
        gate_outcome=_gate_outcome(CreationDisposition.HUMAN_CONFIRMED),
    )

    restored = definition_record_from_row(record.to_dict())

    assert restored == record


def test_record_round_trips_through_json_text() -> None:
    record = stamp_definition(content={"code": "x"}, taint_state=TurnTaintState.empty())

    assert definition_record_from_row(json.dumps(record.to_dict())) == record


def test_record_matches_only_the_content_it_describes() -> None:
    record = stamp_definition(content={"code": "x"}, taint_state=TurnTaintState.empty())

    assert record.matches({"code": "x"})
    assert not record.matches({"code": "y"})


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "not-a-record",
        {},
        {"version": "definition_v0", "content_hash": "sha256:x"},
        {"version": "definition_v1", "content_hash": "sha256:x"},
        {
            "version": "definition_v1",
            "taint_metadata": {"version": "runtime_v2", "max_tier": "trusted_user"},
            "content_hash": "sha256:x",
            "disposition": "not-a-disposition",
        },
    ],
)
def test_unreadable_records_resolve_to_nothing(raw: object) -> None:
    assert definition_record_from_row(raw) is None


def test_stored_record_without_disposition_is_readable() -> None:
    record = DefinitionRecord(
        taint_metadata=TurnTaintState.empty().to_metadata(),
        content_hash=definition_content_hash({"code": "x"}),
        disposition=None,
    )

    assert definition_record_from_row(record.to_dict()) == record


def test_untracked_authoring_turn_stamps_unknown_external() -> None:
    """A turn that cannot prove it was clean must not fire as trusted intent."""
    state = authoring_taint_state(None)

    assert state.max_tier is SourceTrustTier.UNKNOWN_EXTERNAL


def test_tracked_authoring_turn_stamps_what_the_turn_read() -> None:
    tracker = InMemoryTurnTaintTracker(_tainted_state())

    assert authoring_taint_state(tracker).max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert (
        authoring_taint_state(InMemoryTurnTaintTracker()).max_tier
        is SourceTrustTier.TRUSTED_USER
    )


@pytest.mark.parametrize(
    ("disposition", "cures"),
    [
        (CreationDisposition.HUMAN_CONFIRMED, True),
        (CreationDisposition.JUDGE_ALLOWED, True),
        (CreationDisposition.CLEAN, False),
        (CreationDisposition.JUDGE_CONFIRM_REQUIRED, False),
        (CreationDisposition.JUDGE_DENIED, False),
    ],
)
def test_only_a_real_decision_about_the_definition_cures(
    disposition: CreationDisposition,
    cures: bool,
) -> None:
    assert disposition.cures is cures


@pytest.mark.parametrize(
    "mode",
    ["observe", "enforce"],
)
def test_a_judge_allow_cures_in_every_mode(mode: str) -> None:
    """The reviewer computes the same verdict whether or not it could have blocked."""
    record = stamp_definition(
        content={"code": "x"},
        taint_state=_tainted_state(),
        gate_outcome=_gate_outcome(CreationDisposition.JUDGE_ALLOWED, mode=mode),
    )

    assert record.cures


def test_an_unapproved_escalation_and_a_denial_resolve_as_absent() -> None:
    for disposition in (
        CreationDisposition.JUDGE_CONFIRM_REQUIRED,
        CreationDisposition.JUDGE_DENIED,
    ):
        record = stamp_definition(
            content={"code": "x"},
            taint_state=_tainted_state(),
            gate_outcome=_gate_outcome(disposition),
        )

        assert not record.cures


def test_gate_provenance_round_trips_for_audit() -> None:
    gate = GateProvenance(
        layer=GateLayer.STATIC_RULE,
        mode="observe",
        reviewer_revision="gemini-3.7-flash/2026-09",
        verdict_id="verdict-1",
    )
    record = stamp_definition(
        content={"code": "x"},
        taint_state=_tainted_state(),
        gate_outcome=DefinitionGateOutcome(
            disposition=CreationDisposition.JUDGE_ALLOWED, gate=gate
        ),
    )

    restored = definition_record_from_row(record.to_dict())

    assert restored is not None
    assert restored.gate == gate


def test_a_record_without_gate_provenance_is_readable() -> None:
    record = stamp_definition(content={"code": "x"}, taint_state=TurnTaintState.empty())

    assert definition_record_from_row(record.to_dict()) == record
    assert record.gate is None
