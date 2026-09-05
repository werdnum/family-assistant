"""The tier-admission gate: who may ask for which tier, and what that records.

Every path into a run resolves through ``resolve_model_selection``, so these
cover the whole authorization surface for explicit tier selection: an inline
profile admits nothing, a user is bounded by the tiers the profile may be run
on, and a model is bounded by the narrower automatic list.
"""

import pytest

from family_assistant.config_models import (
    ModelTierConfig,
    ProcessingConfig,
    RetryModelConfig,
    ServiceProfile,
)
from family_assistant.llm.model_selection import (
    ModelSelectionRequest,
    ModelTierEligibility,
    ModelTierNotPermitted,
    ModelTierOption,
    ResolvedModelSelection,
    resolve_model_selection,
)

TIERS = {
    "standard": ModelTierConfig(
        label="Standard",
        description="Everyday work.",
        chain=[RetryModelConfig(provider="google", model="gemini-3.8-flash")],
    ),
    "deep": ModelTierConfig(
        label="Deep",
        chain=[RetryModelConfig(provider="openai", model="gpt-5.6-sol")],
    ),
    "frontier": ModelTierConfig(
        chain=[RetryModelConfig(provider="anthropic", model="claude-fable-5")],
    ),
}


def _eligibility(
    default_tier: str | None = "standard",
    allowed: list[str] | None = None,
    auto: list[str] | None = None,
) -> ModelTierEligibility:
    profile = ServiceProfile(
        id="test_profile",
        processing_config=(
            ProcessingConfig(model_tier=default_tier)
            if default_tier is not None
            else ProcessingConfig(llm_model="claude-opus-5")
        ),
        allowed_model_tiers=allowed,
        auto_model_tiers=auto,
    )
    return ModelTierEligibility.from_profile(profile, TIERS)


def test_no_request_resolves_to_the_profile_default() -> None:
    resolved = resolve_model_selection(
        _eligibility(allowed=["standard", "deep"]), None, profile_id="test_profile"
    )

    assert resolved == ResolvedModelSelection(
        tier="standard", requested=None, source="default"
    )


def test_a_request_naming_no_tier_resolves_to_the_profile_default() -> None:
    resolved = resolve_model_selection(
        _eligibility(allowed=["standard", "deep"]),
        ModelSelectionRequest(tier=None, source="user"),
        profile_id="test_profile",
    )

    assert resolved.tier == "standard"
    assert resolved.source == "default"


def test_a_pinned_profile_resolves_to_no_tier_at_all() -> None:
    """An inline model is not a tier, and must not be reported as one."""
    resolved = resolve_model_selection(
        _eligibility(default_tier=None), None, profile_id="pinned"
    )

    assert resolved.tier is None
    assert resolved.source == "default"


def test_a_pinned_profile_refuses_any_selection() -> None:
    with pytest.raises(ModelTierNotPermitted, match="does not support model tier"):
        resolve_model_selection(
            _eligibility(default_tier=None),
            ModelSelectionRequest(tier="deep", source="user"),
            profile_id="pinned",
        )


def test_a_user_may_select_any_allowed_tier() -> None:
    resolved = resolve_model_selection(
        _eligibility(allowed=["standard", "deep", "frontier"], auto=["standard"]),
        ModelSelectionRequest(tier="frontier", source="user"),
        profile_id="test_profile",
    )

    assert resolved == ResolvedModelSelection(
        tier="frontier", requested="frontier", source="user"
    )


def test_a_user_may_not_select_a_tier_outside_the_allowed_list() -> None:
    with pytest.raises(ModelTierNotPermitted) as excinfo:
        resolve_model_selection(
            _eligibility(allowed=["standard", "deep"]),
            ModelSelectionRequest(tier="frontier", source="user"),
            profile_id="test_profile",
        )

    assert excinfo.value.requested_tier == "frontier"
    assert excinfo.value.eligible_tiers == ("standard", "deep")


def test_a_profile_without_an_allowed_list_admits_only_its_default() -> None:
    with pytest.raises(ModelTierNotPermitted, match="Tiers it accepts: standard"):
        resolve_model_selection(
            _eligibility(),
            ModelSelectionRequest(tier="deep", source="user"),
            profile_id="test_profile",
        )


def test_a_model_may_select_within_the_automatic_list() -> None:
    resolved = resolve_model_selection(
        _eligibility(
            allowed=["standard", "deep", "frontier"], auto=["standard", "deep"]
        ),
        ModelSelectionRequest(tier="deep", source="model"),
        profile_id="test_profile",
    )

    assert resolved.tier == "deep"
    assert resolved.source == "model"


def test_a_model_may_not_select_a_tier_only_a_user_may_choose() -> None:
    """The whole point of the second list: `frontier` needs a person."""
    with pytest.raises(ModelTierNotPermitted, match="from another profile") as excinfo:
        resolve_model_selection(
            _eligibility(
                allowed=["standard", "deep", "frontier"], auto=["standard", "deep"]
            ),
            ModelSelectionRequest(tier="frontier", source="model"),
            profile_id="test_profile",
        )

    assert excinfo.value.eligible_tiers == ("standard", "deep")


def test_a_model_selecting_on_a_profile_with_no_automatic_list_gets_the_default() -> (
    None
):
    resolved = resolve_model_selection(
        _eligibility(allowed=["standard", "deep"]),
        ModelSelectionRequest(tier="standard", source="model"),
        profile_id="test_profile",
    )

    assert resolved.tier == "standard"


def test_a_model_may_not_escalate_where_no_automatic_list_is_configured() -> None:
    with pytest.raises(ModelTierNotPermitted):
        resolve_model_selection(
            _eligibility(allowed=["standard", "deep"]),
            ModelSelectionRequest(tier="deep", source="model"),
            profile_id="test_profile",
        )


def test_selecting_the_default_tier_explicitly_keeps_its_source() -> None:
    """ "I chose Standard" and "I chose nothing" authorize differently later."""
    resolved = resolve_model_selection(
        _eligibility(allowed=["standard", "deep"]),
        ModelSelectionRequest(tier="standard", source="user"),
        profile_id="test_profile",
    )

    assert resolved.source == "user"
    assert resolved.requested == "standard"


def test_eligibility_presents_tiers_in_configured_order_with_labels() -> None:
    """Surfaces render this list, so its order and names are the contract."""
    eligibility = _eligibility(allowed=["frontier", "standard"])

    assert eligibility.selectable == (
        ModelTierOption(id="standard", label="Standard", description="Everyday work."),
        ModelTierOption(id="frontier", label="frontier", description=None),
    )


def test_a_resolved_selection_survives_a_json_round_trip() -> None:
    selection = ResolvedModelSelection(tier="deep", requested="deep", source="model")

    assert ResolvedModelSelection.from_json(selection.to_json()) == selection


def test_a_persisted_selection_with_an_unknown_source_is_refused() -> None:
    """A half-understood envelope is a run whose models are unknown."""
    with pytest.raises(ValueError, match="unknown source"):
        ResolvedModelSelection.from_json({"tier": "deep", "source": "vibes"})


def test_a_persisted_selection_ignores_a_key_it_does_not_know() -> None:
    """A row written by a deployment that records more still has to load.

    A run enqueued by a newer deployment must not fail on the older one that
    picks it up: the models it should run on are stated by the keys both
    understand.
    """
    loaded = ResolvedModelSelection.from_json({
        "tier": "deep",
        "requested": "deep",
        "source": "user",
        "something_a_later_deployment_records": "whatever",
    })

    assert loaded == ResolvedModelSelection(
        tier="deep", requested="deep", source="user"
    )


def test_a_persisted_selection_without_routing_fields_loads_as_unrouted() -> None:
    """Which is what a run enqueued before Auto existed was."""
    loaded = ResolvedModelSelection.from_json({
        "tier": "standard",
        "requested": None,
        "source": "default",
    })

    assert loaded.routing_outcome is None
    assert loaded.routing_would_choose is None
    assert loaded.classifier_model is None


def test_a_persisted_routing_outcome_round_trips() -> None:
    """The envelope is how a queued run carries what Auto decided for it."""
    original = ResolvedModelSelection(
        tier="standard",
        requested=None,
        source="default",
        routing_outcome="decided",
        routing_would_choose="deep",
        classifier_model="gemini-3.8-flash",
    )

    assert ResolvedModelSelection.from_json(original.to_json()) == original


def test_a_persisted_selection_refuses_an_outcome_that_is_not_one() -> None:
    """A half-understood envelope is a run whose models are unknown."""
    with pytest.raises(ValueError, match="routing outcome"):
        ResolvedModelSelection.from_json({
            "tier": "standard",
            "requested": None,
            "source": "default",
            "routing_outcome": "probably_fine",
        })


def test_auto_is_bounded_by_the_same_list_a_delegating_model_is() -> None:
    """Choosing Auto authorizes that range; it does not widen it.

    `frontier` stays an explicit human choice, so the classifier cannot reach
    it however the request is phrased.
    """
    eligibility = _eligibility(
        allowed=["standard", "deep", "frontier"], auto=["standard", "deep"]
    )
    resolved = resolve_model_selection(
        eligibility,
        ModelSelectionRequest(tier="deep", source="auto"),
        profile_id="test_profile",
    )
    assert resolved.tier == "deep"
    assert resolved.source == "auto"

    with pytest.raises(ModelTierNotPermitted) as refusal:
        resolve_model_selection(
            eligibility,
            ModelSelectionRequest(tier="frontier", source="auto"),
            profile_id="test_profile",
        )
    assert refusal.value.eligible_tiers == ("standard", "deep")


def test_the_automatic_options_are_the_selectable_ones_the_list_names() -> None:
    """One derivation, so the classifier's offer and the gate cannot disagree."""
    eligibility = _eligibility(
        allowed=["standard", "deep", "frontier"], auto=["standard", "deep"]
    )

    assert [option.id for option in eligibility.auto_options] == ["standard", "deep"]
