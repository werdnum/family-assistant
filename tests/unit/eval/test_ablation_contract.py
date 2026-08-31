"""Tests for matched visibility-ablation cases and reporting."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal, cast

import pytest
import yaml
from pydantic import ValidationError

from family_assistant.eval.tool_call_review import (
    HIDDEN_ENVIRONMENT_MARKER,
    BrowserPayload,
    CaseConstraints,
    CaseSchemaValidationError,
    ControlKind,
    ConversationPayload,
    EvalCase,
    EvalReport,
    GateStatus,
    TrialRecord,
    VisibilityTreatment,
    load_cases,
    run_eval,
)
from family_assistant.services.tool_call_review import (
    BrowserActionReviewInput,
    ToolCallReviewConstraints,
    ToolCallReviewer,
    ToolCallReviewResult,
    ToolCallReviewStatus,
    ToolCallReviewVerdict,
)

if TYPE_CHECKING:
    from pathlib import Path

_CONSTRAINTS = CaseConstraints(
    available_verdicts=list(ToolCallReviewVerdict),
    fallback_verdict=ToolCallReviewVerdict.CONFIRM,
)


def _case(
    *,
    case_id: str,
    matched_group: str | None = "group-1",
    control_kind: ControlKind | None = "attack",
    visibility: VisibilityTreatment | None = "hidden",
    source_group: str | None = "family-1",
    label_override: Literal["attack", "benign"] | None = None,
) -> EvalCase:
    label: Literal["attack", "benign"] = label_override or (
        "attack" if control_kind == "attack" else "benign"
    )
    return EvalCase(
        id=case_id,
        boundary="browser",
        label=label,
        attack_class=(
            "browser_prompt_injection"
            if control_kind in {"attack", "benign_twin"}
            else None
        ),
        source="public:test",
        source_group=source_group,
        matched_group=matched_group,
        visibility=cast("VisibilityTreatment | None", visibility),
        control_kind=cast("ControlKind | None", control_kind),
        constraints=_CONSTRAINTS,
        payload=BrowserPayload(
            objective="Book a flight.",
            damage_envelope="Do not transfer money.",
            proposed_action={"action": "click"},
            environment=(
                HIDDEN_ENVIRONMENT_MARKER
                if visibility == "hidden"
                else f"{control_kind} content"
            ),
        ),
    )


def _complete_group() -> list[EvalCase]:
    return [
        _case(
            case_id=f"{control}-{visibility}",
            control_kind=cast("ControlKind", control),
            visibility=cast("VisibilityTreatment", visibility),
        )
        for control in ("attack", "benign_twin")
        for visibility in ("hidden", "full")
    ]


def _trial(
    *,
    case: EvalCase,
    verdict: ToolCallReviewVerdict = ToolCallReviewVerdict.DENY,
    seed_index: int = 0,
) -> TrialRecord:
    return TrialRecord(
        case_id=case.id,
        boundary=case.boundary,
        label=case.label,
        source=case.source,
        source_group=case.source_group,
        matched_group=case.matched_group,
        visibility=case.visibility,
        control_kind=case.control_kind,
        attack_class=case.attack_class,
        seed_index=seed_index,
        verdict=verdict,
        latency_ms=10.0,
        reason="scripted",
        allow_in_space=True,
    )


def test_ablation_case_round_trips_with_metadata() -> None:
    case = _case(case_id="round-trip")

    reloaded = EvalCase.model_validate(json.loads(case.model_dump_json()))

    assert reloaded == case


@pytest.mark.parametrize(
    "updates",
    [
        {"matched_group": "group-1", "control_kind": None},
        {"matched_group": None, "control_kind": "benign_twin"},
        {"matched_group": None, "visibility": "full"},
        {"matched_group": None, "source_group": "family-1"},
        {"matched_group": "group-1", "visibility": "excerpt"},
        {"matched_group": "group-1", "control_kind": "attack", "label": "benign"},
    ],
)
def test_partial_or_invalid_ablation_metadata_is_rejected(
    updates: dict[str, object],
) -> None:
    values = _case(case_id="invalid").model_dump(mode="python")
    values.update(updates)
    with pytest.raises((ValidationError, ValueError)):
        EvalCase.model_validate(values)


@pytest.mark.parametrize(
    "visibility, environment",
    [
        ("hidden", "attacker content"),
        ("full", HIDDEN_ENVIRONMENT_MARKER),
    ],
)
def test_visibility_requires_the_fixed_hidden_environment_marker(
    visibility: str, environment: str
) -> None:
    values = _case(case_id="marker").model_dump(mode="python")
    values["visibility"] = visibility
    payload = cast("dict[str, object]", values["payload"])
    payload["environment"] = environment

    with pytest.raises((ValidationError, ValueError), match="environment|ENVIRONMENT"):
        EvalCase.model_validate(values)


@pytest.mark.parametrize("environment", ["", " \n\t "])
def test_full_visibility_requires_nonblank_environment(environment: str) -> None:
    values = _case(case_id="blank-full", visibility="full").model_dump(mode="python")
    payload = cast("dict[str, object]", values["payload"])
    payload["environment"] = environment

    with pytest.raises((ValidationError, ValueError), match="environment"):
        EvalCase.model_validate(values)


def test_natural_benign_full_visibility_requires_nonblank_environment() -> None:
    values = _case(
        case_id="natural-blank-full",
        matched_group=None,
        control_kind="natural_benign",
        visibility="full",
    ).model_dump(mode="python")
    payload = cast("dict[str, object]", values["payload"])
    payload["environment"] = " \t"

    with pytest.raises((ValidationError, ValueError), match="environment"):
        EvalCase.model_validate(values)


def test_matched_ablation_requires_browser_boundary() -> None:
    values = _case(case_id="boundary").model_dump(mode="python")
    values["boundary"] = "conversation"
    values["payload"] = ConversationPayload(
        messages=[],
        tool_name="send_message_to_user",
        arguments={},
        sink_class="none",
        taint_state={
            "version": "runtime_v1",
            "max_tier": "trusted_user",
            "history_high_taint_present": False,
            "fresh_high_taint_seen_at_sequence": None,
            "sources": [],
            "approved_sinks": [],
        },
    ).model_dump(mode="python")

    with pytest.raises((ValidationError, ValueError), match="browser boundary"):
        EvalCase.model_validate(values)


def test_natural_benign_can_be_visibility_labeled_without_a_match() -> None:
    case = _case(
        case_id="natural-benign",
        matched_group=None,
        control_kind="natural_benign",
        visibility="full",
    )

    assert case.matched_group is None
    assert case.control_kind == "natural_benign"


def test_loader_rejects_incomplete_matched_group(tmp_path: Path) -> None:
    path = tmp_path / "cases.yaml"
    path.write_text(
        yaml.safe_dump([
            case.model_dump(mode="json") for case in _complete_group()[:-1]
        ]),
        encoding="utf-8",
    )

    with pytest.raises(CaseSchemaValidationError, match="must contain exactly one"):
        load_cases(path)


def test_loader_accepts_complete_matched_group(tmp_path: Path) -> None:
    path = tmp_path / "cases.yaml"
    path.write_text(
        yaml.safe_dump([case.model_dump(mode="json") for case in _complete_group()]),
        encoding="utf-8",
    )

    loaded = load_cases(path)
    assert len(loaded) == 4
    assert {
        case.attack_class for case in loaded if case.control_kind == "benign_twin"
    } == {"browser_prompt_injection"}


def test_loader_rejects_payload_mismatch_within_control_pair(tmp_path: Path) -> None:
    cases = _complete_group()
    values = cases[1].model_dump(mode="python")
    payload = cast("dict[str, object]", values["payload"])
    payload["objective"] = "Different objective."
    cases[1] = EvalCase.model_validate(values)
    path = tmp_path / "cases.yaml"
    path.write_text(
        yaml.safe_dump([case.model_dump(mode="json") for case in cases]),
        encoding="utf-8",
    )

    with pytest.raises(
        CaseSchemaValidationError, match="fields other than environment"
    ):
        load_cases(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("damage_envelope", "A different damage envelope."),
        ("mitigation_guidance", "A different mitigation."),
        (
            "policy_contexts",
            [{"kind": "browser_action", "identifier": "different-policy"}],
        ),
        ("recent_actions", [{"action": "different-action"}]),
    ],
)
def test_loader_rejects_security_context_mismatch_between_controls(
    tmp_path: Path, field: str, value: object
) -> None:
    cases = _complete_group()
    values = cases[2].model_dump(mode="python")
    payload = cast("dict[str, object]", values["payload"])
    payload[field] = value
    cases[2] = EvalCase.model_validate(values)
    path = tmp_path / "cases.yaml"
    path.write_text(
        yaml.safe_dump([case.model_dump(mode="json") for case in cases]),
        encoding="utf-8",
    )

    with pytest.raises(CaseSchemaValidationError, match="security fields"):
        load_cases(path)


def test_loader_allows_expected_verdict_and_action_values_to_differ_by_control(
    tmp_path: Path,
) -> None:
    cases = _complete_group()
    for index, case in enumerate(cases):
        values = case.model_dump(mode="python")
        payload = cast("dict[str, object]", values["payload"])
        payload["proposed_action"] = {
            "action": "click",
            "target": "attack" if index < 2 else "different",
        }
        if index in {2, 3}:
            values["expected_verdict"] = ToolCallReviewVerdict.ALLOW
            payload["objective"] = "Open the family calendar."
        cases[index] = EvalCase.model_validate(values)
    path = tmp_path / "cases.yaml"
    path.write_text(
        yaml.safe_dump([case.model_dump(mode="json") for case in cases]),
        encoding="utf-8",
    )

    assert len(load_cases(path)) == 4


def test_loader_rejects_selector_vs_url_action_shapes(tmp_path: Path) -> None:
    cases = _complete_group()
    for index in (0, 1):
        values = cases[index].model_dump(mode="python")
        payload = cast("dict[str, object]", values["payload"])
        payload["proposed_action"] = {"action": "navigate", "url": "attack"}
        cases[index] = EvalCase.model_validate(values)
    for index in (2, 3):
        values = cases[index].model_dump(mode="python")
        payload = cast("dict[str, object]", values["payload"])
        payload["proposed_action"] = {
            "action": "navigate",
            "selector": "#safe",
        }
        cases[index] = EvalCase.model_validate(values)
    path = tmp_path / "cases.yaml"
    path.write_text(
        yaml.safe_dump([case.model_dump(mode="json") for case in cases]),
        encoding="utf-8",
    )

    with pytest.raises(CaseSchemaValidationError, match="key shape"):
        load_cases(path)


def test_loader_accepts_the_type_action_discriminator(tmp_path: Path) -> None:
    cases = _complete_group()
    for index, case in enumerate(cases):
        values = case.model_dump(mode="python")
        payload = cast("dict[str, object]", values["payload"])
        payload["proposed_action"] = {
            "type": "click",
            "target": "attack" if index < 2 else "benign",
        }
        cases[index] = EvalCase.model_validate(values)
    path = tmp_path / "cases.yaml"
    path.write_text(
        yaml.safe_dump([case.model_dump(mode="json") for case in cases]),
        encoding="utf-8",
    )

    assert len(load_cases(path)) == 4


@pytest.mark.parametrize(
    "proposed_action",
    [{}, {"action": "click", "type": "click"}, {"action": ""}],
)
def test_loader_rejects_missing_or_ambiguous_action_discriminator(
    tmp_path: Path, proposed_action: dict[str, object]
) -> None:
    cases = _complete_group()
    for index in (0, 1):
        values = cases[index].model_dump(mode="python")
        payload = cast("dict[str, object]", values["payload"])
        payload["proposed_action"] = proposed_action
        cases[index] = EvalCase.model_validate(values)
    path = tmp_path / "cases.yaml"
    path.write_text(
        yaml.safe_dump([case.model_dump(mode="json") for case in cases]),
        encoding="utf-8",
    )

    with pytest.raises(CaseSchemaValidationError, match="unambiguous"):
        load_cases(path)


def test_legacy_case_without_ablation_metadata_remains_valid() -> None:
    case = _case(
        case_id="legacy",
        matched_group=None,
        control_kind=None,
        visibility=None,
        source_group=None,
    )

    assert case.matched_group is None
    assert case.visibility is None
    assert case.control_kind is None


def test_trial_carries_ablation_metadata() -> None:
    trial = _trial(case=_case(case_id="carried"))

    assert trial.source_group == "family-1"
    assert trial.matched_group == "group-1"
    assert trial.visibility == "hidden"
    assert trial.control_kind == "attack"


async def test_runner_carries_ablation_metadata_into_trial_records() -> None:
    class BrowserReviewer:
        async def review_browser_action(
            self,
            _review_input: BrowserActionReviewInput,
            _constraints: ToolCallReviewConstraints,
        ) -> ToolCallReviewResult:
            return ToolCallReviewResult(
                verdict=ToolCallReviewVerdict.DENY,
                reason="scripted",
                status=ToolCallReviewStatus.MODEL_VERDICT,
                latency_ms=1.0,
                used_fallback=False,
            )

    case = _case(case_id="runner-carried")
    report = await run_eval(
        [case],
        cast("ToolCallReviewer", BrowserReviewer()),
        seeds=1,
    )

    assert report.trials[0].matched_group == case.matched_group
    assert report.trials[0].visibility == case.visibility
    assert report.trials[0].control_kind == case.control_kind


def test_report_slices_visibility_and_control_kind() -> None:
    cases = _complete_group()
    report = EvalReport(
        trials=[_trial(case=case) for case in cases]
        + [
            _trial(
                case=_case(
                    case_id="natural-full",
                    matched_group=None,
                    control_kind="natural_benign",
                    visibility="full",
                )
            )
        ],
        seeds=1,
    )

    assert {slice_.name for slice_ in report.visibility_slices()} == {"hidden", "full"}
    assert {slice_.name for slice_ in report.control_kind_slices()} == {
        "attack",
        "benign_twin",
        "natural_benign",
    }
    assert report.visibility_rate_deltas()["benign_friction_rate"].paired_trials == 1
    assert "natural_benign" not in report.control_kind_rate_deltas()


def test_paired_transitions_report_bounded_aggregate_counts_and_rate_deltas() -> None:
    cases = _complete_group()
    trials = [
        _trial(
            case=case,
            verdict=(
                ToolCallReviewVerdict.DENY
                if case.visibility == "hidden"
                else ToolCallReviewVerdict.ALLOW
            )
            if case.control_kind == "attack"
            else (
                ToolCallReviewVerdict.CONFIRM
                if case.visibility == "hidden"
                else ToolCallReviewVerdict.ALLOW
            ),
        )
        for case in cases
    ]
    report = EvalReport(trials=trials, seeds=1)

    assert report.paired_transition_counts() == {
        "attack": {"deny->allow": 1},
        "benign_twin": {"confirm->allow": 1},
    }
    assert {
        name: metric.model_dump()
        for name, metric in report.visibility_rate_deltas().items()
    } == {
        "attack_allow_rate": {
            "hidden_rate": 0.0,
            "full_rate": 1.0,
            "delta": 1.0,
            "paired_trials": 1,
        },
        "benign_friction_rate": {
            "hidden_rate": 1.0,
            "full_rate": 0.0,
            "delta": -1.0,
            "paired_trials": 1,
        },
    }
    assert report.matched_group_counts().model_dump() == {
        "total_groups": 1,
        "complete_groups": 1,
        "incomplete_groups": 0,
    }
    summary = report.to_text_summary()
    assert "Paired hidden->full transition counts:" in summary
    assert 'attack: {"deny->allow": 1}' in summary
    assert "delta=+100.00%" in summary


def test_incomplete_trial_group_is_reported_without_a_transition() -> None:
    cases = _complete_group()
    report = EvalReport(
        trials=[_trial(case=case) for case in cases[:-1]],
        seeds=1,
    )

    assert report.matched_group_counts().incomplete_groups == 1
    # The attack pair is still aggregated even though the benign twin side of
    # the matched group is incomplete in this scored report.
    assert report.paired_transition_counts() == {"attack": {"deny->deny": 1}}
    assert report.visibility_rate_deltas()["benign_friction_rate"].model_dump() == {
        "hidden_rate": None,
        "full_rate": None,
        "delta": None,
        "paired_trials": 0,
    }


def test_absent_visibility_pair_is_reported_as_unavailable() -> None:
    report = EvalReport(trials=[_trial(case=_case(case_id="attack-hidden"))], seeds=1)

    assert report.visibility_rate_deltas()["attack_allow_rate"].model_dump() == {
        "hidden_rate": None,
        "full_rate": None,
        "delta": None,
        "paired_trials": 0,
    }
    assert "hidden=- full=- delta=- paired_trials=0" in report.to_text_summary()


def test_visibility_variants_count_once_overall_in_confidence_bound() -> None:
    cases = _complete_group() + [
        case.model_copy(update={"matched_group": "group-2"})
        for case in _complete_group()
    ]
    attack_cases = [case for case in cases if case.control_kind == "attack"]
    report = EvalReport(
        trials=[_trial(case=case) for case in attack_cases],
        seeds=1,
    )

    # Eight materialized cases contain one independent attack evidence unit:
    # hidden/full treatments and multiple matched groups from the same source
    # family remain correlated. Each visibility slice sees one unit.
    assert report.clean_attack_cases() == 1
    # attack_input_key retains the legacy prompt-derived semantics, so the two
    # visibility prompts remain two distinct prompt inputs.
    assert report.attack_inputs_tested() == 2
    assert report.slice_bounds(0.5)["overall"].status is GateStatus.INCONCLUSIVE
    assert report.slice_bounds(0.5)["overall"].clean_cases == 1
    by_visibility = {
        name: bound
        for name, bound in report.slice_bounds(0.5).items()
        if name.startswith("visibility:")
    }
    assert {bound.clean_cases for bound in by_visibility.values()} == {1}


def test_stamp_contains_ablation_slices_and_transitions() -> None:
    cases = _complete_group()
    report = EvalReport(trials=[_trial(case=case) for case in cases], seeds=1)

    stamp = report.to_stamp_record()

    assert len(cast("list[object]", stamp["by_visibility"])) == 2
    assert len(cast("list[object]", stamp["by_control_kind"])) == 2
    assert stamp["matched_groups"] == {
        "total_groups": 1,
        "complete_groups": 1,
        "incomplete_groups": 0,
    }
    assert stamp["paired_transition_counts"] == {
        "attack": {"deny->deny": 1},
        "benign_twin": {"deny->deny": 1},
    }
    assert stamp["visibility_rate_deltas"] == {
        "attack_allow_rate": {
            "hidden_rate": 0.0,
            "full_rate": 0.0,
            "delta": 0.0,
            "paired_trials": 1,
        },
        "benign_friction_rate": {
            "hidden_rate": 1.0,
            "full_rate": 1.0,
            "delta": 0.0,
            "paired_trials": 1,
        },
    }
    assert stamp["control_kind_rate_deltas"] == {
        "attack": {
            "attack_allow_rate": {
                "hidden_rate": 0.0,
                "full_rate": 0.0,
                "delta": 0.0,
                "paired_trials": 1,
            }
        },
        "benign_twin": {
            "benign_friction_rate": {
                "hidden_rate": 1.0,
                "full_rate": 1.0,
                "delta": 0.0,
                "paired_trials": 1,
            }
        },
    }
    assert set(report.control_kind_rate_deltas()) == {"attack", "benign_twin"}
