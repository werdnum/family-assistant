"""The shipped tiers must serve the same clients the inline chains did.

Moving a profile onto a tier is a refactor of where the models are written
down, not a change of model, provider or request parameters. The assertions
below are the concrete dicts the previous inline `retry_config` blocks
produced, so a tier that quietly resolves to something else fails here rather
than in production.

The pinned profiles are asserted from the other direction: they must still name
their model inline, because their runtime is coupled to one provider or one API
surface and a tier would offer to replace it.
"""

from typing import TYPE_CHECKING, cast

import pytest

from family_assistant.assistant import Assistant
from family_assistant.config_models import AppConfig
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.llm.model_selection import ModelTierEligibility
from family_assistant.llm.model_tiers import (
    resolve_profile_llm_model,
    resolve_tier_client_config,
    validate_profile_model_tier,
)
from family_assistant.llm.providers.anthropic_client import AnthropicClient
from tests.unit.conftest import shipped_profile

if TYPE_CHECKING:
    from family_assistant.llm import LLMInterface


# ast-grep-ignore: no-dict-any - Factory config has varying provider keys.
def _client_config_for(config: AppConfig, profile_id: str) -> dict[str, object]:
    """The configuration the assistant would build this profile's client from."""
    assistant = Assistant(config)
    profile = shipped_profile(config, profile_id)
    tier = validate_profile_model_tier(profile, config.model_tiers)
    model = resolve_profile_llm_model(profile.processing_config, tier, config.model)
    return assistant._build_profile_llm_client_config(profile, model, tier)


# ast-grep-ignore: no-dict-any - Factory config has varying provider keys.
def _tier_config_for(config: AppConfig, profile_id: str) -> dict[str, object]:
    """The configuration this profile's tier resolves to, with no assistant."""
    profile = shipped_profile(config, profile_id)
    tier = validate_profile_model_tier(profile, config.model_tiers)
    assert tier is not None, f"profile {profile_id!r} does not name a model tier"
    return resolve_tier_client_config(tier, config.llm_parameters)


@pytest.mark.parametrize(
    "profile_id",
    [
        pytest.param("default_assistant", id="default-assistant"),
        pytest.param("camera_analyst", id="camera-analyst"),
    ],
)
def test_standard_tier_profiles_keep_the_gemini_terra_chain(
    shipped_config: AppConfig, profile_id: str
) -> None:
    """Through the assistant, so the tier reaching the client is asserted too."""
    assert _client_config_for(shipped_config, profile_id) == {
        "retry_config": {
            "primary": {
                "provider": "google",
                "model": "gemini-3.8-flash",
                "model_parameters": shipped_config.llm_parameters,
            },
            "fallback": {
                "provider": "openai",
                "model": "gpt-5.6-terra",
                "model_parameters": shipped_config.llm_parameters,
            },
        }
    }


def test_complex_tasks_keeps_the_sol_fable_chain(shipped_config: AppConfig) -> None:
    assert _tier_config_for(shipped_config, "complex_tasks") == {
        "retry_config": {
            "primary": {
                "provider": "openai",
                "model": "gpt-5.6-sol",
                "model_parameters": shipped_config.llm_parameters,
            },
            "fallback": {
                "provider": "anthropic",
                "model": "claude-fable-5",
                "model_parameters": shipped_config.llm_parameters,
            },
        }
    }


def test_a_profile_inheriting_the_default_tier_keeps_the_default_chain(
    shipped_config: AppConfig,
) -> None:
    """The chain `default_profile_settings` used to carry is now `standard`."""
    config = _tier_config_for(shipped_config, "email_intake")

    assert config == {
        "retry_config": {
            "primary": {
                "provider": "google",
                "model": "gemini-3.8-flash",
                "model_parameters": shipped_config.llm_parameters,
            },
            "fallback": {
                "provider": "openai",
                "model": "gpt-5.6-terra",
                "model_parameters": shipped_config.llm_parameters,
            },
        }
    }


def test_sol_reasoning_effort_still_comes_from_the_global_map(
    shipped_config: AppConfig,
) -> None:
    """The `deep` tier declares no overrides, so nothing shadows the global entry."""
    deep = shipped_config.model_tiers["deep"]

    assert all(entry.llm_parameters is None for entry in deep.chain)
    assert shipped_config.llm_parameters["gpt-5.6-sol"]["reasoning_effort"] == "high"


def test_frontier_tier_is_a_single_fable_client_at_xhigh(
    shipped_config: AppConfig,
) -> None:
    resolved = resolve_tier_client_config(
        shipped_config.model_tiers["frontier"], shipped_config.llm_parameters
    )

    assert resolved["provider"] == "anthropic"
    assert resolved["model"] == "claude-fable-5"
    assert resolved["model_parameters"]["claude-fable-5"] == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "xhigh"},
        "max_tokens": 16000,
    }


def test_frontier_thinking_config_reaches_the_anthropic_client(
    shipped_config: AppConfig,
) -> None:
    """End to end: shipped tier -> factory -> the params a request carries.

    Fable takes `adaptive` + output_config and rejects the previous
    generation's `enabled` + `budget_tokens`, which is a 400 mid-conversation
    rather than a startup error, so the shape is asserted here.
    """
    resolved = resolve_tier_client_config(
        shipped_config.model_tiers["frontier"], shipped_config.llm_parameters
    )
    client = LLMClientFactory.create_client({**resolved, "api_key": "test-key"})

    assert isinstance(client, AnthropicClient)
    params = client._get_model_specific_params("claude-fable-5")
    assert params["thinking"] == {"type": "adaptive"}
    assert params["output_config"] == {"effort": "xhigh"}


def test_the_global_map_still_configures_nothing_for_fable(
    shipped_config: AppConfig,
) -> None:
    """Fable is `deep`'s fallback, where it must inherit no thinking config.

    The `frontier` overlay is per entry precisely so enabling thinking for the
    tier that exists for it does not reach the tier that merely falls back to
    the same model.
    """
    assert "claude-fable-5" not in shipped_config.llm_parameters

    deep_fallback = resolve_tier_client_config(
        shipped_config.model_tiers["deep"], shipped_config.llm_parameters
    )["retry_config"]["fallback"]
    client = LLMClientFactory.create_client({**deep_fallback, "api_key": "test-key"})

    assert isinstance(client, AnthropicClient)
    assert "thinking" not in client._get_model_specific_params("claude-fable-5")


@pytest.mark.parametrize(
    "profile_id",
    [
        pytest.param("browser_visual_profile", id="computer-use"),
        pytest.param("research", id="deep-research"),
        pytest.param("research_max", id="deep-research-max"),
        pytest.param("coder", id="antigravity"),
        pytest.param("media_analyst", id="gemini-only-media"),
    ],
)
def test_provider_coupled_profiles_stay_pinned_to_an_inline_model(
    shipped_config: AppConfig, profile_id: str
) -> None:
    processing_config = shipped_profile(shipped_config, profile_id).processing_config

    assert processing_config.model_tier is None
    assert processing_config.llm_model is not None


def test_every_shipped_profile_passes_tier_validation(
    shipped_config: AppConfig,
) -> None:
    """Startup validates each profile; nothing shipped may fail it."""
    for profile in shipped_config.service_profiles:
        validate_profile_model_tier(profile, shipped_config.model_tiers)


def test_a_tiered_profile_gets_one_client_per_tier_it_may_run_on(
    shipped_config: AppConfig, provider_api_keys: None
) -> None:
    """Built at startup, so a tier's chain and credentials fail the boot, not
    the first request that reaches for it."""
    assistant = Assistant(shipped_config)
    profile = shipped_profile(shipped_config, "default_assistant")
    eligibility = ModelTierEligibility.from_profile(profile, shipped_config.model_tiers)

    default_client, clients = assistant._create_profile_llm_clients(
        profile,
        resolve_profile_llm_model(
            profile.processing_config,
            validate_profile_model_tier(profile, shipped_config.model_tiers),
            shipped_config.model,
        ),
        validate_profile_model_tier(profile, shipped_config.model_tiers),
        eligibility,
    )

    assert set(clients) == {"standard", "deep", "frontier"}
    assert isinstance(clients["frontier"], AnthropicClient)
    # The default tier's entry *is* the service's default client, not a second
    # client built the same way.
    assert default_client is clients["standard"]


def test_a_test_override_can_replace_one_tiers_client(
    shipped_config: AppConfig,
) -> None:
    """`"<profile>@<tier>"` is the seam a test uses to tell the tiers apart.

    A bare profile id keeps overriding every tier, which is what a test that
    does not care about tiers wants -- and what stops one from reaching a real
    provider by accident.
    """
    only_deep = cast("LLMInterface", object())
    everything = cast("LLMInterface", object())
    assistant = Assistant(
        shipped_config,
        llm_client_overrides={
            "default_assistant@deep": only_deep,
            "default_assistant": everything,
        },
    )
    profile = shipped_profile(shipped_config, "default_assistant")
    eligibility = ModelTierEligibility.from_profile(profile, shipped_config.model_tiers)

    _, clients = assistant._create_profile_llm_clients(
        profile,
        resolve_profile_llm_model(
            profile.processing_config,
            validate_profile_model_tier(profile, shipped_config.model_tiers),
            shipped_config.model,
        ),
        validate_profile_model_tier(profile, shipped_config.model_tiers),
        eligibility,
    )

    assert clients["deep"] is only_deep
    assert clients["standard"] is everything
    assert clients["frontier"] is everything
