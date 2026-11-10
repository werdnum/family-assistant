"""The shipped `coder` profile must reach the managed agent, not a chat model.

Each way this breaks gives a plausible-looking answer rather than an error: a
fallback chat model answering from its own knowledge, or a profile that is
advertised, gated or credentialed differently from what it claims. Those are
pinned here. That a run is reasoned with its tier's model, and that the agent
receives the profile's own prompt, is read back from the real agent in
tests/integration/llm/test_shipped_coder_profile_integration.py.
"""

import pytest

from family_assistant.assistant import validate_antigravity_agent_config
from family_assistant.config_models import (
    AppConfig,
    ProcessingConfig,
    RetryConfig,
    RetryModelConfig,
)
from family_assistant.llm.model_selection import (
    ModelSelectionRequest,
    ModelTierEligibility,
    ModelTierNotPermitted,
    resolve_model_selection,
)
from family_assistant.llm.model_tiers import validate_profile_model_tier
from family_assistant.security.taint import (
    SinkClass,
    SourceTrustTier,
    TaintPolicyConfig,
    TaintPolicyEvaluator,
    TaintPolicyMode,
    TaintPolicyOutcome,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
    merge_taint_policy_config,
)
from family_assistant.telegram.commands import normalize_slash_command
from family_assistant.telegram.service import build_profile_slash_command_map
from tests.unit.conftest import shipped_profile


def _coder_eligibility(shipped_config: AppConfig) -> ModelTierEligibility:
    return ModelTierEligibility.from_profile(
        shipped_profile(shipped_config, "coder"), shipped_config.model_tiers
    )


_STRONG_CODER_TIERS = ["gemini_4_argon", "gemini_4_carbon"]


@pytest.mark.parametrize("tier", _STRONG_CODER_TIERS)
def test_the_tier_picker_offers_strong_models_on_coder(
    shipped_config: AppConfig, tier: str
) -> None:
    """The web and iOS picker lists the profile's selectable tiers, and only those.

    A person's request for a delegation preset is admitted too, but nothing
    offers it to them, so admission alone would not show the choice exists.
    """
    assert tier in _coder_eligibility(shipped_config).selectable_ids


@pytest.mark.parametrize("tier", _STRONG_CODER_TIERS)
def test_auto_routing_never_puts_coder_on_a_strong_model(
    shipped_config: AppConfig, tier: str
) -> None:
    """These cost many times Flash, so only someone's explicit choice reaches them."""
    with pytest.raises(ModelTierNotPermitted):
        resolve_model_selection(
            _coder_eligibility(shipped_config),
            ModelSelectionRequest(tier=tier, source="auto"),
            profile_id="coder",
        )


def test_slash_coder_reaches_the_coder_profile(shipped_config: AppConfig) -> None:
    command_map = build_profile_slash_command_map(shipped_config)

    assert command_map[normalize_slash_command("/coder")] == "coder"


def test_shipped_coder_profile_configures_no_sandbox_credentials(
    shipped_config: AppConfig,
) -> None:
    """Out of the box the sandbox holds no credential, so the profile keeps [C] only.

    `antigravity_config.environment` can inject a credential into the sandbox's
    egress proxy — a GitHub App token, say — which adds [B] to a profile that
    already reads the open web. That is a deployment's decision to make in its
    own config, so shipping it enabled here would hand every deployment the
    widening silently.
    """
    profile = shipped_profile(shipped_config, "coder")
    antigravity_config = profile.processing_config.antigravity_config

    assert antigravity_config is not None
    assert antigravity_config.environment is None


def test_shipped_coder_profile_grants_no_family_assistant_tools(
    shipped_config: AppConfig,
) -> None:
    """The agent works only from the request; it holds no [B] access.

    A deny-by-default `tools_policy` is not enough on its own: `global_tools_policy`
    is injected at the `profile` layer, which outranks the `defaults` layer this
    policy occupies, so the globally granted tools have to be withheld
    explicitly or the profile is advertised as holding them.
    """
    profile = shipped_profile(shipped_config, "coder")
    assert profile.tools_policy is not None
    assert profile.tools_policy.default_decision == "deny"
    assert set(profile.excluded_global_tools) == {
        "read_text_attachment",
        "jq_query",
        "report_technical_problem",
        "finish_thread",
    }


def test_shipped_coder_profile_is_a_sandbox_network_sink(
    shipped_config: AppConfig,
) -> None:
    """The profile declares the sink that gates reaching it at all.

    Without the declaration, a delegation to it is classified as an ordinary
    delegation and untrusted content could direct a code-execution agent --
    the thing the shipped matrix already denies for `spawn_worker`.
    """
    processing_config = shipped_profile(shipped_config, "coder").processing_config
    assert processing_config.taint_sink_class is SinkClass.SANDBOX_NETWORK


def test_shipped_coder_profile_does_not_override_the_rollout_mode(
    shipped_config: AppConfig,
) -> None:
    """The sink declaration rides the deployment's rollout; it does not preempt it.

    A profile-level `enforce` would apply the context-free tier x sink matrix to
    this one profile while the deployment is still measuring that matrix's
    false-positive rate -- and it is the friction that keeps the rollout in
    `observe` in the first place (see
    docs/design/runtime-taint-enforcement-operational-findings.md). Worse for
    this cell specifically: ambient high-tier prompt notes raise whole profiles
    to `unknown_external` irrespective of the request, so an override would
    refuse ordinary `/coder` turns, and the correction that document proposes
    for interactive `unknown_external -> sandbox_network` is confirmation
    rather than the hard denial an override produces.
    """
    profile = shipped_profile(shipped_config, "coder")

    merged = merge_taint_policy_config(
        base=shipped_config.taint_policy, profile=profile.taint_policy
    )

    assert merged.mode is shipped_config.taint_policy.mode


def test_the_shipped_matrix_adjudicates_a_sandbox_run_untrusted_content() -> None:
    """The reviewer may allow the run; an unavailable reviewer asks a human."""
    evaluator = TaintPolicyEvaluator(TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE))
    state = TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.EMAIL,
            source_id="msg-1",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset(),
            reason="Inbound email.",
        )
    )

    evaluation = evaluator.evaluate(state=state, sink_class=SinkClass.SANDBOX_NETWORK)

    assert evaluation.effective_outcome is TaintPolicyOutcome.ADJUDICATE
    assert evaluation.verdict_floor is None
    assert evaluation.fallback_outcome is TaintPolicyOutcome.CONFIRM


def test_shipped_coder_profile_passes_its_own_startup_validation(
    shipped_config: AppConfig,
) -> None:
    """What ships must survive the guards that reject unrunnable combinations."""
    profile = shipped_profile(shipped_config, "coder")
    validate_profile_model_tier(profile, shipped_config.model_tiers)
    validate_antigravity_agent_config(
        profile.id, profile.processing_config, shipped_config.model
    )


def test_the_agent_in_a_retry_chain_is_rejected() -> None:
    """A fallback chat model would answer instead of running the task.

    The retry format carries no `antigravity_config`, so even as the primary
    the agent would run with none of its settings.
    """
    with pytest.raises(ValueError, match="retry_config, which is unsupported"):
        validate_antigravity_agent_config(
            "misconfigured",
            ProcessingConfig(
                retry_config=RetryConfig(
                    primary=RetryModelConfig(model="antigravity-preview-09-2026"),
                    fallback=RetryModelConfig(model="gemini-3.8-flash"),
                ),
            ),
            "gemini-3.8-flash",
        )
