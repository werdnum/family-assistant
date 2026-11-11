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
from family_assistant.llm.custom_instructions import CustomInstructionsClient
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.llm.model_selection import ModelTierEligibility
from family_assistant.llm.model_tiers import (
    resolve_entry_client_config,
    resolve_entry_model_parameters,
    resolve_profile_llm_model,
    resolve_tier_client_config,
    validate_profile_model_tier,
)
from family_assistant.llm.providers.anthropic_client import AnthropicClient
from family_assistant.llm.providers.openai_client import OpenAIClient
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


# ast-grep-ignore: no-dict-any - LLM params are provider-specific and genuinely arbitrary
def _standard_fallback_parameters(config: AppConfig) -> dict[str, dict[str, object]]:
    return resolve_entry_model_parameters(
        config.model_tiers["standard"].chain[1], config.llm_parameters
    )


def test_standard_tier_falls_back_to_sonnet_at_medium_effort(
    shipped_config: AppConfig,
) -> None:
    """Sonnet 5.5 rejects `enabled` + `budget_tokens`, so its shape is asserted."""
    resolved = resolve_tier_client_config(
        shipped_config.model_tiers["standard"], shipped_config.llm_parameters
    )["retry_config"]
    wrapped = LLMClientFactory.create_client({
        **resolved["fallback"],
        "api_key": "test-key",
    })
    assert isinstance(wrapped, CustomInstructionsClient)
    fallback = wrapped.inner

    assert isinstance(fallback, AnthropicClient)
    assert fallback.model == "claude-sonnet-5-5"
    assert fallback._get_model_specific_params(fallback.model) == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium"},
    }


def test_sonnet_preset_runs_as_the_standard_fallback_does(
    shipped_config: AppConfig,
) -> None:
    """An experiment arm on the preset compares models, not efforts."""
    preset = shipped_config.model_tiers["claude_sonnet_5_5"]
    standard_fallback = shipped_config.model_tiers["standard"].chain[1]

    assert len(preset.chain) == 1
    assert preset.chain[0].model == standard_fallback.model
    assert resolve_entry_model_parameters(
        preset.chain[0], shipped_config.llm_parameters
    ) == _standard_fallback_parameters(shipped_config)


@pytest.mark.parametrize(
    ("tier_id", "entry_index"),
    [
        pytest.param("standard", 1, id="standard-fallback"),
        pytest.param("claude_sonnet_5_5", 0, id="preset"),
    ],
)
def test_sonnet_chat_entries_carry_the_completion_policy(
    shipped_config: AppConfig, tier_id: str, entry_index: int
) -> None:
    """Sonnet in everyday chat is told to run the checks it identifies."""
    entry = shipped_config.model_tiers[tier_id].chain[entry_index]
    client = LLMClientFactory.create_client({
        **resolve_entry_client_config(entry, shipped_config.llm_parameters),
        "api_key": "test-key",
    })

    assert isinstance(client, CustomInstructionsClient)
    assert "Finish the job in this turn." in client.custom_instructions


def test_browser_sonnet_fallback_has_no_completion_policy(
    shipped_config: AppConfig,
) -> None:
    """A browsing run has its own prompt; the chat policy stays out of it."""
    entry = shipped_config.model_tiers["browser"].chain[1]
    client = LLMClientFactory.create_client({
        **resolve_entry_client_config(entry, shipped_config.llm_parameters),
        "api_key": "test-key",
    })

    assert isinstance(client, AnthropicClient)


@pytest.mark.parametrize(
    "preset", ["claude_sonnet_5_5", "claude_haiku_5_5", "gpt_6_1_terra"]
)
def test_default_assistant_can_run_the_standard_candidate_presets(
    shipped_config: AppConfig, preset: str
) -> None:
    """An experiment arm must be a tier the profile can run on."""
    assistant = shipped_profile(shipped_config, "default_assistant")
    eligibility = ModelTierEligibility.from_profile(
        assistant, shipped_config.model_tiers
    )

    assert preset in {option.id for option in eligibility.runnable}


@pytest.mark.parametrize(
    "profile_id",
    [
        pytest.param("default_assistant", id="default-assistant"),
        pytest.param("engineer", id="engineer"),
    ],
)
def test_standard_tier_profiles_keep_the_gemini_sonnet_chain(
    shipped_config: AppConfig, profile_id: str
) -> None:
    """Through the assistant, so the tier reaching the client is asserted too."""
    assert _client_config_for(shipped_config, profile_id) == {
        "retry_config": {
            "primary": {
                "provider": "google",
                "model": "gemini-3.8-flash",
                "model_parameters": shipped_config.llm_parameters,
                "flex": True,
            },
            "fallback": {
                "provider": "anthropic",
                "model": "claude-sonnet-5-5",
                "model_parameters": _standard_fallback_parameters(shipped_config),
            },
        }
    }


def test_complex_tasks_runs_the_opus_sol_chain(shipped_config: AppConfig) -> None:
    assert _tier_config_for(shipped_config, "complex_tasks") == {
        "retry_config": {
            "primary": {
                "provider": "anthropic",
                "model": "claude-opus-5-5",
                "model_parameters": shipped_config.llm_parameters,
            },
            "fallback": {
                "provider": "openai",
                "model": "gpt-6.1-sol",
                "model_parameters": shipped_config.llm_parameters,
            },
        }
    }


def test_engineer_offers_every_tier_with_standard_as_its_default(
    shipped_config: AppConfig,
) -> None:
    """The engineer has the assistant's tier setup; a person may pick up from there.

    `frontier` is absent from the automatic range for the same reason as on
    the assistant: it is chosen when somebody decides a request is worth the
    spend, not inferred.
    """
    profile = shipped_profile(shipped_config, "engineer")

    assert profile.processing_config.model_tier == "standard"
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
                "flex": True,
            },
            "fallback": {
                "provider": "anthropic",
                "model": "claude-sonnet-5-5",
                "model_parameters": _standard_fallback_parameters(shipped_config),
            },
        }
    }


def test_deep_tier_reasoning_still_comes_from_the_global_map(
    shipped_config: AppConfig,
) -> None:
    """The `deep` tier declares no overrides, so nothing shadows the global entries."""
    deep = shipped_config.model_tiers["deep"]

    assert all(entry.llm_parameters is None for entry in deep.chain)
    assert shipped_config.llm_parameters["gpt-6.1-sol"]["reasoning_effort"] == "high"
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
    # Left to the client, which sizes it by call path.
    assert "max_tokens" not in params


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
    assert client.model == "claude-fable-5-2"
    assert client._get_model_specific_params(client.model) == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
    }


def test_the_global_map_still_configures_nothing_for_fable(
    shipped_config: AppConfig,
) -> None:
    """Fable 5.2 must inherit no thinking config wherever a deployment names it.

    The `frontier` overlay is per entry precisely so enabling thinking for the
    tier that exists for it does not reach other models in the same family.
    The global map matches by substring, so the check is on the family prefix:
    a `claude-fable-5` entry would reach 5.1 as well.
    """
    assert not any(
        key.startswith("claude-fable-") for key in shipped_config.llm_parameters
    )


def test_any_tiered_profile_can_be_pinned_to_a_shipped_preset(
    shipped_config: AppConfig,
) -> None:
    """The presets live in `default_profile_settings`, not on each profile.

    So a specialist such as complex_tasks can be run on a named model without a
    list of its own, while a profile coupled to one model inline (media_analyst
    needs Gemini to read media) inherits none: a preset there would offer to
    replace it.
    """
    complex_tasks = shipped_profile(shipped_config, "complex_tasks")
    media_analyst = shipped_profile(shipped_config, "media_analyst")

    assert "gemini_4_argon" in (complex_tasks.delegation_model_tiers or [])
    assert validate_profile_model_tier(complex_tasks, shipped_config.model_tiers)
    assert media_analyst.delegation_model_tiers is None


def test_profiles_share_one_client_per_tier(
    shipped_config: AppConfig, provider_api_keys: None
) -> None:
    """A tier's client is configured from the tier alone, so it is built once.

    The shipped presets reach every tiered profile, so a client per profile per
    tier would multiply identical clients by the profile count.
    """
    assistant = Assistant(shipped_config, llm_client_overrides={})

    def clients_for(profile_id: str) -> dict[str, object]:
        profile = shipped_profile(shipped_config, profile_id)
        tier = validate_profile_model_tier(profile, shipped_config.model_tiers)
        model = resolve_profile_llm_model(
            profile.processing_config, tier, shipped_config.model
        )
        eligibility = ModelTierEligibility.from_profile(
            profile, shipped_config.model_tiers
        )
        _, clients = assistant._create_profile_llm_clients(
            profile, model, tier, eligibility
        )
        return dict(clients)

    assistant_clients = clients_for("default_assistant")
    complex_clients = clients_for("complex_tasks")

    assert complex_clients["gemini_4_argon"] is assistant_clients["gemini_4_argon"]
    assert complex_clients["deep"] is assistant_clients["deep"]


@pytest.mark.parametrize(
    "profile_id",
    [
        pytest.param("browser_profile", id="browser"),
        pytest.param("authenticated_browser_profile", id="authenticated-browser"),
        pytest.param("credential_browser_profile", id="credential-browser"),
    ],
)
def test_snapshot_browser_profiles_run_the_browser_tier(
    shipped_config: AppConfig, profile_id: str
) -> None:
    profile = shipped_profile(shipped_config, profile_id)
    assert profile.processing_config.model_tier == "browser"


def test_browser_tier_runs_sol_with_a_sonnet_fallback_at_medium_effort(
    shipped_config: AppConfig,
) -> None:
    """End to end: shipped tier -> factory -> the params each entry's request carries.

    Sonnet 5.5 rejects `enabled` + `budget_tokens` mid-conversation, and the
    browser entry's medium must win over the global `gpt-6.1-sol` high, so both
    entries' parameters are asserted where they land.
    """
    resolved = resolve_tier_client_config(
        shipped_config.model_tiers["browser"], shipped_config.llm_parameters
    )["retry_config"]
    primary = LLMClientFactory.create_client({
        **resolved["primary"],
        "api_key": "test-key",
    })
    fallback = LLMClientFactory.create_client({
        **resolved["fallback"],
        "api_key": "test-key",
    })

    assert isinstance(primary, OpenAIClient)
    assert primary.model == "gpt-6.1-sol"
    assert primary._get_model_specific_params(primary.model) == {
        "reasoning_effort": "medium"
    }
    assert isinstance(fallback, AnthropicClient)
    assert fallback.model == "claude-sonnet-5-5"
    assert fallback._get_model_specific_params(fallback.model) == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium"},
    }


def test_every_shipped_flex_entry_is_one_its_client_can_honour(
    shipped_config: AppConfig,
) -> None:
    """The startup check, over every tier, including ones no profile reaches."""
    flex_entries = [
        (tier_id, entry)
        for tier_id, tier in shipped_config.model_tiers.items()
        for entry in tier.chain
        if entry.flex
    ]

    assert flex_entries, "the shipped configuration enables Flex somewhere"
    for _tier_id, entry in flex_entries:
        LLMClientFactory.validate_flex_config(
            resolve_entry_client_config(entry, shipped_config.llm_parameters)
        )
