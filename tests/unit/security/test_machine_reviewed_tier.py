"""The ``machine_reviewed`` tier and the two questions it answers differently.

See docs/design/ambient-note-admission-at-write-time.md, milestone 1.
"""

from __future__ import annotations

import pytest

from family_assistant.security.taint import (
    SinkClass,
    SourceTrustTier,
    TaintPolicyConfig,
    TaintPolicyEvaluator,
    TaintPolicyOutcome,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
    is_admissible_for_reuse,
    is_externally_authored,
)


def _state(*tiers: SourceTrustTier) -> TurnTaintState:
    state = TurnTaintState.empty()
    for index, tier in enumerate(tiers):
        state = state.add_source(
            TaintSource(
                source_type=TaintSourceType.NOTE,
                source_id=f"source-{index}",
                tier=tier,
                labels=frozenset(),
                reason="test source",
            )
        )
    return state


def test_tier_sits_between_trusted_internal_and_known_contact() -> None:
    assert (
        SourceTrustTier.TRUSTED_INTERNAL
        < SourceTrustTier.MACHINE_REVIEWED
        < SourceTrustTier.KNOWN_CONTACT
    )


@pytest.mark.parametrize(
    ("tier", "external", "reusable"),
    [
        (SourceTrustTier.TRUSTED_USER, False, True),
        (SourceTrustTier.TRUSTED_INTERNAL, False, True),
        (SourceTrustTier.MACHINE_REVIEWED, True, True),
        (SourceTrustTier.KNOWN_CONTACT, True, False),
        (SourceTrustTier.RECOGNIZED_MACHINE, True, False),
        (SourceTrustTier.UNKNOWN_EXTERNAL, True, False),
        (None, True, False),
    ],
)
def test_authorship_and_reuse_predicates(
    tier: SourceTrustTier | None, external: bool, reusable: bool
) -> None:
    assert is_externally_authored(tier) is external
    assert is_admissible_for_reuse(tier) is reusable


def test_max_rule_lets_fresh_external_content_win() -> None:
    state = _state(SourceTrustTier.MACHINE_REVIEWED, SourceTrustTier.UNKNOWN_EXTERNAL)
    assert state.max_tier is SourceTrustTier.UNKNOWN_EXTERNAL

    reviewed_only = _state(
        SourceTrustTier.TRUSTED_USER, SourceTrustTier.MACHINE_REVIEWED
    )
    assert reviewed_only.max_tier is SourceTrustTier.MACHINE_REVIEWED


def test_round_trips_through_metadata_by_name() -> None:
    state = _state(SourceTrustTier.MACHINE_REVIEWED)
    metadata = state.to_metadata()

    assert metadata.get("max_tier") == "machine_reviewed"
    assert [source["tier"] for source in metadata.get("sources", [])] == [
        "machine_reviewed"
    ]
    restored = TurnTaintState.from_metadata(metadata)
    assert restored.max_tier is SourceTrustTier.MACHINE_REVIEWED
    assert restored.sources == state.sources


def test_integer_tier_in_configuration_is_rejected_naming_the_key() -> None:
    with pytest.raises(ValueError, match=r"operator_minimum\.2.*known_contact"):
        TaintPolicyConfig.model_validate({
            "operator_minimum": {2: {"artifact_write": "confirm"}}
        })
    with pytest.raises(ValueError, match="high_taint_tier"):
        TaintPolicyConfig.model_validate({"high_taint_tier": 4})
    with pytest.raises(ValueError, match=r"matrix_overrides\.3"):
        TaintPolicyConfig.model_validate({
            "matrix_overrides": {"3": {"artifact_write": "deny"}}
        })


def test_named_tier_in_configuration_parses_as_before() -> None:
    config = TaintPolicyConfig.model_validate({
        "operator_minimum": {"known_contact": {"artifact_write": "confirm"}}
    })
    assert SourceTrustTier.KNOWN_CONTACT in config.operator_minimum


def test_machine_reviewed_cannot_be_configured_on_its_own() -> None:
    with pytest.raises(ValueError, match="takes trusted_internal's policy cells"):
        TaintPolicyConfig.model_validate({
            "matrix_overrides": {"machine_reviewed": {"artifact_write": "deny"}}
        })


def _outcomes(
    evaluator: TaintPolicyEvaluator, tier: SourceTrustTier
) -> dict[SinkClass, tuple[TaintPolicyOutcome, TaintPolicyOutcome | None]]:
    state = _state(tier)
    result = {}
    for sink in SinkClass:
        evaluation = evaluator.evaluate(state=state, sink_class=sink)
        result[sink] = (evaluation.requested_outcome, evaluation.fallback_outcome)
    return result


@pytest.mark.parametrize(
    "config",
    [
        TaintPolicyConfig(),
        TaintPolicyConfig.model_validate({
            "matrix_overrides": {
                "trusted_internal": {
                    "known_user_message": {
                        "outcome": "adjudicate",
                        "fallback": "confirm",
                    }
                }
            }
        }),
        TaintPolicyConfig.model_validate({
            "matrix_overrides": {"trusted_user": {"artifact_write": "confirm"}}
        }),
        TaintPolicyConfig.model_validate({
            "operator_minimum": {
                "trusted_user": {"arbitrary_external_message": "confirm"}
            }
        }),
    ],
    ids=["shipped", "override", "trusted-user-override", "operator-minimum"],
)
def test_policy_lookup_resolves_machine_reviewed_as_trusted_internal(
    config: TaintPolicyConfig,
) -> None:
    evaluator = TaintPolicyEvaluator(config)

    assert _outcomes(evaluator, SourceTrustTier.MACHINE_REVIEWED) == _outcomes(
        evaluator, SourceTrustTier.TRUSTED_INTERNAL
    )
