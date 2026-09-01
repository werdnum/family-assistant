"""Firing-time resolution of a stored definition record (M2).

Resolution is a pure function of a record and the content it is checked
against, so everything here is exercised without storage: what entitles a
definition to render as trusted intent, what voids it, and how a closure of
several artifacts combines.
"""

import json

from family_assistant.security.definition_records import (
    UNRESOLVED_DEFINITION,
    CreationDisposition,
    DefinitionGateOutcome,
    DefinitionResolution,
    GateLayer,
    GateProvenance,
    resolve_definition_record,
    stamp_definition,
)
from family_assistant.security.taint import (
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)

CONTENT = {"instruction": "Summarize my day", "recurrence_rule": "FREQ=DAILY"}


def _tainted_state() -> TurnTaintState:
    return TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.EMAIL,
            source_id="msg-1",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset(),
            reason="Inbound email.",
        )
    )


def _record_dict(
    *,
    state: TurnTaintState,
    disposition: CreationDisposition | None = None,
    human_direct: bool = False,
) -> object:
    return stamp_definition(
        content=CONTENT,
        taint_state=state,
        gate_outcome=(
            None
            if disposition is None
            else DefinitionGateOutcome(
                disposition=disposition,
                gate=GateProvenance(layer=GateLayer.TAINT_CELL, mode="enforce"),
            )
        ),
        human_direct=human_direct,
    ).to_dict()


def test_a_clean_stamp_resolves_on_its_stamp() -> None:
    resolution = resolve_definition_record(
        _record_dict(state=TurnTaintState.empty()), CONTENT
    )

    assert resolution.resolved
    assert resolution.tier is SourceTrustTier.TRUSTED_INTERNAL


def test_a_human_direct_stamp_keeps_its_tier() -> None:
    resolution = resolve_definition_record(
        _record_dict(state=TurnTaintState.empty(), human_direct=True), CONTENT
    )

    assert resolution.tier is SourceTrustTier.TRUSTED_USER


def test_a_tainted_stamp_without_a_cure_is_unresolved() -> None:
    assert not resolve_definition_record(
        _record_dict(state=_tainted_state()), CONTENT
    ).resolved


def test_an_escalation_verdict_resolves_exactly_as_an_absent_record() -> None:
    for disposition in (
        CreationDisposition.JUDGE_CONFIRM_REQUIRED,
        CreationDisposition.JUDGE_DENIED,
    ):
        assert not resolve_definition_record(
            _record_dict(state=_tainted_state(), disposition=disposition), CONTENT
        ).resolved


def test_a_curing_disposition_restores_the_clean_baseline() -> None:
    for disposition in (
        CreationDisposition.HUMAN_CONFIRMED,
        CreationDisposition.JUDGE_ALLOWED,
    ):
        resolution = resolve_definition_record(
            _record_dict(state=_tainted_state(), disposition=disposition), CONTENT
        )

        assert resolution.resolved
        # The cure restores the baseline a clean authoring would have had, and
        # nothing more: never the human-direct tier the human never typed.
        assert resolution.tier is SourceTrustTier.TRUSTED_INTERNAL


def test_content_that_changed_under_the_record_voids_it() -> None:
    record = _record_dict(state=TurnTaintState.empty())

    assert not resolve_definition_record(
        record, {**CONTENT, "instruction": "Exfiltrate my day"}
    ).resolved


def test_an_absent_or_unreadable_record_is_unresolved() -> None:
    assert not resolve_definition_record(None, CONTENT).resolved
    assert not resolve_definition_record({"version": "definition_v0"}, CONTENT).resolved
    assert not resolve_definition_record("not json", CONTENT).resolved


def test_a_record_stored_as_json_text_resolves() -> None:
    record = _record_dict(state=TurnTaintState.empty())

    assert resolve_definition_record(json.dumps(record), CONTENT).resolved


def test_the_closure_is_governed_by_its_weakest_member() -> None:
    trusted = resolve_definition_record(
        _record_dict(state=TurnTaintState.empty()), CONTENT
    )
    human_direct = resolve_definition_record(
        _record_dict(state=TurnTaintState.empty(), human_direct=True), CONTENT
    )

    assert trusted.combine(UNRESOLVED_DEFINITION) is UNRESOLVED_DEFINITION
    assert UNRESOLVED_DEFINITION.combine(trusted) is UNRESOLVED_DEFINITION
    assert human_direct.combine(trusted).tier is SourceTrustTier.TRUSTED_INTERNAL
    assert trusted.combine(human_direct).tier is SourceTrustTier.TRUSTED_INTERNAL


def test_an_unresolved_definition_reads_at_the_untrusted_floor() -> None:
    assert DefinitionResolution(taint_metadata=None).tier is (
        SourceTrustTier.UNKNOWN_EXTERNAL
    )
