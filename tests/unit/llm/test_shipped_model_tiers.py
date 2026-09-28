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

import pytest

from family_assistant.assistant import Assistant
from family_assistant.config_models import AppConfig
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.llm.model_tiers import (
    resolve_profile_llm_model,
    resolve_tier_client_config,
    validate_profile_model_tier,
)
from family_assistant.llm.providers.anthropic_client import AnthropicClient
from tests.unit.conftest import shipped_profile


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


@pytest.mark.parametrize(
    "profile_id",
    [
        pytest.param("complex_tasks", id="complex-tasks"),
        pytest.param("engineer", id="engineer"),
    ],
)
def test_deep_tier_profiles_run_the_opus_sol_chain(
    shipped_config: AppConfig, profile_id: str
) -> None:
    assert _tier_config_for(shipped_config, profile_id) == {
        "retry_config": {
            "primary": {
                "provider": "anthropic",
                "model": "claude-opus-5-5",
                "model_parameters": shipped_config.llm_parameters,
            },
            "fallback": {
                "provider": "openai",
                "model": "gpt-6-sol",
                "model_parameters": shipped_config.llm_parameters,
            },
        }
    }


def test_engineer_offers_every_tier_with_deep_as_its_floor(
    shipped_config: AppConfig,
) -> None:
    """Diagnosis runs on `deep` by default; a person may still pick either way.

    `frontier` is absent from the automatic range for the same reason as on
    the assistant: it is chosen when somebody decides a request is worth the
    spend, not inferred.
    """
    profile = shipped_profile(shipped_config, "engineer")

    assert profile.processing_config.model_tier == "deep"
    assert profile.processing_config.llm_model is None
    assert profile.processing_config.provider is None
    assert profile.allowed_model_tiers == ["standard", "deep", "frontier"]
    assert profile.auto_model_tiers == ["standard", "deep"]
    assert profile.auto_routing_guidance


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


def test_deep_tier_reasoning_still_comes_from_the_global_map(
    shipped_config: AppConfig,
) -> None:
    """The `deep` tier declares no overrides, so nothing shadows the global entries."""
    deep = shipped_config.model_tiers["deep"]

    assert all(entry.llm_parameters is None for entry in deep.chain)
    assert shipped_config.llm_parameters["gpt-6-sol"]["reasoning_effort"] == "high"
    assert shipped_config.llm_parameters["claude-opus-5-5"]["output_config"] == {
        "effort": "high"
    }


def test_deep_primary_thinking_config_reaches_the_anthropic_client(
    shipped_config: AppConfig,
) -> None:
    """End to end: shipped tier -> factory -> the params an Opus 5.5 request carries.

    Opus 5.5 defaults to `medium` effort, a step below Opus 5, so `high` has
    to arrive explicitly; and it rejects `enabled` + `budget_tokens` with a
    400 mid-conversation, so the thinking shape is asserted too.
    """
    deep_primary = resolve_tier_client_config(
        shipped_config.model_tiers["deep"], shipped_config.llm_parameters
    )["retry_config"]["primary"]
    client = LLMClientFactory.create_client({**deep_primary, "api_key": "test-key"})

    assert isinstance(client, AnthropicClient)
    params = client._get_model_specific_params("claude-opus-5-5")
    assert params["thinking"] == {"type": "adaptive"}
    assert params["output_config"] == {"effort": "high"}
    assert params["max_tokens"] == 16000


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
    assert client.model == "claude-fable-5"
    assert client._get_model_specific_params(client.model) == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "xhigh"},
        "max_tokens": 16000,
    }


def test_the_global_map_still_configures_nothing_for_fable(
    shipped_config: AppConfig,
) -> None:
    """Fable 5.1 must inherit no thinking config wherever a deployment names it.

    The `frontier` overlay is per entry precisely so enabling thinking for the
    tier that exists for it does not reach other models in the same family.
    The global map matches by substring, so the check is on the family prefix:
    a `claude-fable-5` entry would reach 5.1 as well.
    """
    assert not any(
        key.startswith("claude-fable-") for key in shipped_config.llm_parameters
    )
