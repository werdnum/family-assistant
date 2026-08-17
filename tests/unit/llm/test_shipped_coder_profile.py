"""The shipped `coder` profile must reach the managed agent, not a chat model.

Three things can break silently between `defaults.yaml` and an actual agent
run: the profile could name a model that routes to `generateContent` instead of
the Interactions API, its `antigravity_config` could go nowhere (leaving the
agent on whatever model the API defaults to), or a fallback could quietly
answer from a chat model's own knowledge instead of running the task. Each is a
plausible-looking answer rather than an error, so each is pinned here.
"""

import pytest

from family_assistant.assistant import validate_antigravity_agent_config
from family_assistant.config_loader import load_config
from family_assistant.config_models import (
    ProcessingConfig,
    RetryConfig,
    RetryModelConfig,
    ServiceProfile,
)
from family_assistant.llm.messages import UserMessage
from family_assistant.llm.providers.google_genai_client import (
    GoogleGenAIClient,
    is_interactions_agent_model,
)
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


def _shipped_profile(profile_id: str) -> ServiceProfile:
    config = load_config(
        config_file_path="nonexistent-so-only-defaults.yaml",
        load_dotenv_file=False,
    )
    matches = [p for p in config.service_profiles if p.id == profile_id]
    assert matches, f"No shipped profile '{profile_id}'"
    return matches[0]


def test_shipped_coder_profile_runs_gemini_37_flash_on_the_agent() -> None:
    """The profile names the managed agent and pins its reasoning model."""
    profile = _shipped_profile("coder")
    processing_config = profile.processing_config

    assert processing_config.provider == "google"
    assert is_interactions_agent_model(processing_config.llm_model or "") is True
    assert processing_config.antigravity_config is not None
    assert processing_config.antigravity_config.model == "gemini-3.7-flash"
    assert "/coder" in profile.slash_commands


def test_shipped_coder_profile_configures_no_sandbox_credentials() -> None:
    """Out of the box the sandbox holds no credential, so the profile keeps [C] only.

    `antigravity_config.environment` can inject a credential into the sandbox's
    egress proxy — a GitHub App token, say — which adds [B] to a profile that
    already reads the open web. That is a deployment's decision to make in its
    own config, so shipping it enabled here would hand every deployment the
    widening silently.
    """
    profile = _shipped_profile("coder")
    antigravity_config = profile.processing_config.antigravity_config

    assert antigravity_config is not None
    assert antigravity_config.environment is None


def test_shipped_coder_profile_grants_no_family_assistant_tools() -> None:
    """The agent works only from the request; it holds no [B] access.

    A deny-by-default `tools_policy` is not enough on its own: `global_tools_policy`
    is injected at the `profile` layer, which outranks the `defaults` layer this
    policy occupies, so the three globally granted tools have to be withheld
    explicitly or the profile is advertised as holding them.
    """
    profile = _shipped_profile("coder")
    assert profile.tools_policy is not None
    assert profile.tools_policy.default_decision == "deny"
    assert set(profile.excluded_global_tools) == {
        "read_text_attachment",
        "jq_query",
        "report_technical_problem",
    }


def test_shipped_coder_profile_is_a_sandbox_network_sink() -> None:
    """The profile declares the sink that gates reaching it at all.

    Without the declaration, a delegation to it is classified as an ordinary
    delegation and untrusted content could direct a code-execution agent --
    the thing the shipped matrix already denies for `spawn_worker`.
    """
    processing_config = _shipped_profile("coder").processing_config
    assert processing_config.taint_sink_class is SinkClass.SANDBOX_NETWORK


def test_shipped_coder_profile_does_not_override_the_rollout_mode() -> None:
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
    config = load_config(
        config_file_path="nonexistent-so-only-defaults.yaml",
        load_dotenv_file=False,
    )
    profile = next(p for p in config.service_profiles if p.id == "coder")

    merged = merge_taint_policy_config(
        base=config.taint_policy, profile=profile.taint_policy
    )

    assert merged.mode is config.taint_policy.mode


def test_the_shipped_matrix_denies_a_sandbox_run_untrusted_content() -> None:
    """The declaration is only worth anything if the matrix backs it."""
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

    assert evaluation.effective_outcome is TaintPolicyOutcome.DENY


def test_shipped_coder_config_reaches_the_agent_config_payload() -> None:
    """Feeding the shipped values to the client produces the API's agent_config."""
    processing_config = _shipped_profile("coder").processing_config
    assert processing_config.antigravity_config is not None

    client = GoogleGenAIClient(
        api_key="test",
        model=processing_config.llm_model or "",
        antigravity_model=processing_config.antigravity_config.model,
        antigravity_max_total_tokens=processing_config.antigravity_config.max_total_tokens,
    )

    kwargs = client._build_agent_create_kwargs([UserMessage(content="Do the thing.")])
    assert kwargs["agent"] == processing_config.llm_model
    assert kwargs["agent_config"] == {
        "type": "antigravity",
        "model": "gemini-3.7-flash",
    }


def test_shipped_coder_profile_passes_its_own_startup_validation() -> None:
    """What ships must survive the guard that rejects unrunnable combinations."""
    profile = _shipped_profile("coder")
    validate_antigravity_agent_config(
        profile.id, profile.processing_config, profile.processing_config.llm_model or ""
    )


def test_antigravity_config_on_a_non_agent_profile_is_rejected() -> None:
    """Settings that would be silently discarded fail at startup instead."""
    with pytest.raises(ValueError, match="not an Antigravity managed agent"):
        validate_antigravity_agent_config(
            "misconfigured",
            ProcessingConfig(
                llm_model="gemini-3.7-flash",
                antigravity_config={"model": "gemini-3.7-flash"},  # pyright: ignore[reportArgumentType]
            ),
            "gemini-3.7-flash",
        )


def test_antigravity_profile_with_retry_config_is_rejected() -> None:
    """A fallback chat model would answer instead of running the task."""
    with pytest.raises(ValueError, match="retry_config, which is unsupported"):
        validate_antigravity_agent_config(
            "misconfigured",
            ProcessingConfig(
                llm_model="antigravity-preview-05-2026",
                provider="google",
                retry_config=RetryConfig(
                    primary=RetryModelConfig(model="antigravity-preview-05-2026"),
                    fallback=RetryModelConfig(model="gemini-3.7-flash"),
                ),
            ),
            "antigravity-preview-05-2026",
        )


def test_antigravity_named_only_inside_a_retry_chain_is_rejected() -> None:
    """`llm_model` unset leaves the profile on the app default, hiding the agent.

    The retry format carries no `antigravity_config`, and the pollable-service
    selection reads `llm_model` -- so a delegated run would quietly take the
    inline path with the API's default reasoning model.
    """
    with pytest.raises(ValueError, match="retry_config, which is unsupported"):
        validate_antigravity_agent_config(
            "misconfigured",
            ProcessingConfig(
                provider="google",
                retry_config=RetryConfig(
                    primary=RetryModelConfig(model="antigravity-preview-05-2026"),
                    fallback=RetryModelConfig(model="gemini-3.7-flash"),
                ),
            ),
            "gemini-3.7-flash",  # the application default, not the agent
        )


@pytest.mark.parametrize("provider", ["openai", "anthropic", None])
def test_antigravity_profile_on_a_non_google_provider_is_rejected(
    provider: str | None,
) -> None:
    """Another provider's client would send the agent id as a chat model."""
    with pytest.raises(ValueError, match="must be 'google'"):
        validate_antigravity_agent_config(
            "misconfigured",
            ProcessingConfig(
                llm_model="antigravity-preview-05-2026",
                provider=provider,
            ),
            "antigravity-preview-05-2026",
        )
