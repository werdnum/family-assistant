"""Resolving a model tier into the configuration a client is built from.

The load-bearing property is ordering: the global ``llm_parameters`` map is
matched by *substring* in insertion order, so a per-entry override only wins if
its exact model key is re-inserted after every global pattern. Asserting the
resolved dict alone would not catch a merge that put it first, so the parameter
tests go through the provider client's own resolution.
"""

import pytest

from family_assistant.config_models import (
    AntigravityConfig,
    ModelTierConfig,
    ProcessingConfig,
    RemoteA2AConfig,
    RetryModelConfig,
    ServiceProfile,
)
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.llm.model_tiers import (
    resolve_entry_client_config,
    resolve_tier_client_config,
    validate_profile_model_tier,
)
from family_assistant.llm.providers.openai_client import OpenAIClient
from family_assistant.llm.retrying_client import RetryingLLMClient

GLOBAL_PARAMS: dict[str, dict[str, object]] = {
    "gpt-5.6-sol": {"reasoning_effort": "high"},
    "gpt-5.6-terra": {"reasoning_effort": "medium"},
}


def _profile(
    processing_config: ProcessingConfig,
    profile_id: str = "test_profile",
    allowed_model_tiers: list[str] | None = None,
    remote_a2a: RemoteA2AConfig | None = None,
) -> ServiceProfile:
    return ServiceProfile(
        id=profile_id,
        processing_config=processing_config,
        allowed_model_tiers=allowed_model_tiers,
        remote_a2a=remote_a2a,
    )


def test_two_entry_chain_resolves_to_a_retry_configuration() -> None:
    tier = ModelTierConfig(
        chain=[
            RetryModelConfig(provider="openai", model="gpt-5.6-sol"),
            RetryModelConfig(provider="anthropic", model="claude-fable-5"),
        ]
    )

    assert resolve_tier_client_config(tier, GLOBAL_PARAMS) == {
        "retry_config": {
            "primary": {
                "provider": "openai",
                "model": "gpt-5.6-sol",
                "model_parameters": GLOBAL_PARAMS,
            },
            "fallback": {
                "provider": "anthropic",
                "model": "claude-fable-5",
                "model_parameters": GLOBAL_PARAMS,
            },
        }
    }


def test_single_entry_chain_resolves_to_a_plain_client_configuration() -> None:
    """One model is one client, not a retry wrapper around a single model."""
    tier = ModelTierConfig(
        chain=[RetryModelConfig(provider="anthropic", model="claude-fable-5")]
    )

    assert resolve_tier_client_config(tier, GLOBAL_PARAMS) == {
        "provider": "anthropic",
        "model": "claude-fable-5",
        "model_parameters": GLOBAL_PARAMS,
    }


def test_entry_without_a_provider_leaves_detection_to_the_factory() -> None:
    tier = ModelTierConfig(chain=[RetryModelConfig(model="gpt-5.6-sol")])

    assert "provider" not in resolve_tier_client_config(tier, GLOBAL_PARAMS)


def test_per_entry_override_is_applied_after_every_global_pattern() -> None:
    """The overlay must win over a global entry for the same model.

    Resolution walks the map in insertion order and updates on every substring
    match, so an overlay merged in place of the global entry would be overtaken
    by any later pattern that also matches the model.
    """
    # The generic pattern is last and matches the same model, so it is what
    # would overtake an overlay merged into the exact-model entry in place.
    global_params: dict[str, dict[str, object]] = {
        "gpt-5.6-sol": {"reasoning_effort": "high"},
        "gpt-5.6-": {"reasoning_effort": "medium"},
    }
    tier = ModelTierConfig(
        chain=[
            RetryModelConfig(
                provider="openai",
                model="gpt-5.6-sol",
                llm_parameters={"reasoning_effort": "xhigh"},
            )
        ]
    )

    client = LLMClientFactory.create_client({
        **resolve_tier_client_config(tier, global_params),
        "api_key": "test-key",
    })

    assert isinstance(client, OpenAIClient)
    assert client._get_model_specific_params("gpt-5.6-sol") == {
        "reasoning_effort": "xhigh"
    }


def test_per_entry_override_merges_with_the_global_entry_for_that_model() -> None:
    """An overlay tunes the shipped parameters rather than replacing them."""
    entry = RetryModelConfig(
        provider="openai",
        model="gpt-5.6-sol",
        llm_parameters={"max_tokens": 16000},
    )

    resolved = resolve_entry_client_config(
        entry, {"gpt-5.6-sol": {"reasoning_effort": "high"}}
    )

    assert resolved["model_parameters"]["gpt-5.6-sol"] == {
        "reasoning_effort": "high",
        "max_tokens": 16000,
    }


def test_per_entry_override_does_not_mutate_the_global_map() -> None:
    """Every other entry and profile reads the same map object."""
    global_params: dict[str, dict[str, object]] = {
        "gpt-5.6-sol": {"reasoning_effort": "high"}
    }
    entry = RetryModelConfig(
        provider="openai",
        model="gpt-5.6-sol",
        llm_parameters={"reasoning_effort": "xhigh"},
    )

    resolve_entry_client_config(entry, global_params)

    assert global_params == {"gpt-5.6-sol": {"reasoning_effort": "high"}}


def test_per_entry_override_reaches_only_its_own_entry() -> None:
    tier = ModelTierConfig(
        chain=[
            RetryModelConfig(
                provider="openai",
                model="gpt-5.6-sol",
                llm_parameters={"reasoning_effort": "xhigh"},
            ),
            RetryModelConfig(provider="openai", model="gpt-5.6-terra"),
        ]
    )

    resolved = resolve_tier_client_config(tier, GLOBAL_PARAMS)

    assert resolved["retry_config"]["fallback"]["model_parameters"] == GLOBAL_PARAMS


def test_factory_builds_a_retrying_client_from_a_resolved_tier() -> None:
    tier = ModelTierConfig(
        chain=[
            RetryModelConfig(provider="google", model="gemini-3.8-flash"),
            RetryModelConfig(provider="openai", model="gpt-5.6-terra"),
        ]
    )
    resolved = resolve_tier_client_config(tier, GLOBAL_PARAMS)
    resolved["retry_config"]["primary"]["api_key"] = "test-key"
    resolved["retry_config"]["fallback"]["api_key"] = "test-key"

    client = LLMClientFactory.create_client(resolved)

    assert isinstance(client, RetryingLLMClient)
    assert client.primary_model == "gemini-3.8-flash"
    assert client.fallback_model == "gpt-5.6-terra"


def test_chain_longer_than_two_entries_is_rejected() -> None:
    with pytest.raises(ValueError, match="1 or 2 entries"):
        ModelTierConfig(
            chain=[
                RetryModelConfig(model="a"),
                RetryModelConfig(model="b"),
                RetryModelConfig(model="c"),
            ]
        )


def test_chain_entry_without_a_model_is_rejected() -> None:
    with pytest.raises(ValueError, match="must set 'model'"):
        ModelTierConfig(chain=[RetryModelConfig(provider="openai")])


TIERS = {
    "standard": ModelTierConfig(
        chain=[RetryModelConfig(provider="google", model="gemini-3.8-flash")]
    ),
    "deep": ModelTierConfig(
        chain=[RetryModelConfig(provider="openai", model="gpt-5.6-sol")]
    ),
}


def test_validation_returns_the_named_tier() -> None:
    profile = _profile(ProcessingConfig(model_tier="standard"))

    assert validate_profile_model_tier(profile, TIERS) is TIERS["standard"]


def test_validation_returns_none_for_an_inline_model_profile() -> None:
    profile = _profile(
        ProcessingConfig(provider="anthropic", llm_model="claude-opus-5")
    )

    assert validate_profile_model_tier(profile, TIERS) is None


def test_unknown_tier_is_rejected() -> None:
    profile = _profile(ProcessingConfig(model_tier="cheap"), profile_id="reminder")

    with pytest.raises(ValueError, match="'reminder' names unknown model_tier"):
        validate_profile_model_tier(profile, TIERS)


def test_tier_on_a_computer_use_profile_is_rejected() -> None:
    profile = _profile(
        ProcessingConfig(model_tier="standard", enable_computer_use=True),
        profile_id="browser_visual_profile",
    )

    with pytest.raises(ValueError, match="enable_computer_use"):
        validate_profile_model_tier(profile, TIERS)


def test_tier_on_an_antigravity_profile_is_rejected() -> None:
    profile = _profile(
        ProcessingConfig(
            model_tier="standard",
            antigravity_config=AntigravityConfig(model="gemini-3.8-flash"),
        ),
        profile_id="coder",
    )

    with pytest.raises(ValueError, match="antigravity_config"):
        validate_profile_model_tier(profile, TIERS)


def test_tier_naming_an_interactions_agent_model_is_rejected() -> None:
    tiers = {
        "research": ModelTierConfig(
            chain=[
                RetryModelConfig(provider="google", model="gemini-3.8-flash"),
                RetryModelConfig(
                    provider="google", model="deep-research-preview-04-2026"
                ),
            ]
        )
    }
    profile = _profile(ProcessingConfig(model_tier="research"))

    with pytest.raises(ValueError, match="Interactions API agent model"):
        validate_profile_model_tier(profile, tiers)


def test_tier_on_a_remote_a2a_profile_is_rejected() -> None:
    profile = _profile(
        ProcessingConfig(model_tier="standard"),
        profile_id="remote",
        remote_a2a=RemoteA2AConfig(agent_url="https://example.invalid/a2a"),
    )

    with pytest.raises(ValueError, match="remote A2A profile"):
        validate_profile_model_tier(profile, TIERS)


def test_allowed_model_tiers_naming_an_unknown_tier_is_rejected() -> None:
    profile = _profile(
        ProcessingConfig(model_tier="standard"),
        allowed_model_tiers=["standard", "frontier"],
    )

    with pytest.raises(ValueError, match="allows unknown model tier"):
        validate_profile_model_tier(profile, TIERS)


def test_default_tier_outside_the_allowed_list_is_rejected() -> None:
    profile = _profile(
        ProcessingConfig(model_tier="standard"), allowed_model_tiers=["deep"]
    )

    with pytest.raises(ValueError, match="not in its allowed_model_tiers"):
        validate_profile_model_tier(profile, TIERS)


def test_allowed_model_tiers_without_a_tier_is_rejected() -> None:
    """Selection needs a default to return to, which an inline model is not."""
    profile = _profile(
        ProcessingConfig(llm_model="claude-opus-5"), allowed_model_tiers=["deep"]
    )

    with pytest.raises(ValueError, match="allowed_model_tiers without a model_tier"):
        validate_profile_model_tier(profile, TIERS)
