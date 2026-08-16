"""The shipped `antigravity` profile must reach the managed agent, not a chat model.

Three things can break silently between `defaults.yaml` and an actual agent
run: the profile could name a model that routes to `generateContent` instead of
the Interactions API, its `antigravity_config` could go nowhere (leaving the
agent on whatever model the API defaults to), or a fallback could quietly
answer from a chat model's own knowledge instead of running the task. Each is a
plausible-looking answer rather than an error, so each is pinned here.
"""

import pytest

from family_assistant.assistant import validate_antigravity_profile
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


def _shipped_profile(profile_id: str) -> ServiceProfile:
    config = load_config(
        config_file_path="nonexistent-so-only-defaults.yaml",
        load_dotenv_file=False,
    )
    matches = [p for p in config.service_profiles if p.id == profile_id]
    assert matches, f"No shipped profile '{profile_id}'"
    return matches[0]


def test_shipped_antigravity_profile_runs_gemini_37_flash_on_the_agent() -> None:
    """The profile names the managed agent and pins its reasoning model."""
    profile = _shipped_profile("antigravity")
    processing_config = profile.processing_config

    assert processing_config.provider == "google"
    assert is_interactions_agent_model(processing_config.llm_model or "") is True
    assert processing_config.antigravity_config is not None
    assert processing_config.antigravity_config.model == "gemini-3.7-flash"
    assert "/antigravity" in profile.slash_commands


def test_shipped_antigravity_profile_grants_no_family_assistant_tools() -> None:
    """The agent works only from the request; it holds no [B] access."""
    profile = _shipped_profile("antigravity")
    assert profile.tools_policy is not None
    assert profile.tools_policy.default_decision == "deny"


def test_shipped_antigravity_config_reaches_the_agent_config_payload() -> None:
    """Feeding the shipped values to the client produces the API's agent_config."""
    processing_config = _shipped_profile("antigravity").processing_config
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


def test_shipped_antigravity_profile_passes_its_own_startup_validation() -> None:
    """What ships must survive the guard that rejects unrunnable combinations."""
    profile = _shipped_profile("antigravity")
    validate_antigravity_profile(
        profile.id, profile.processing_config, profile.processing_config.llm_model or ""
    )


def test_antigravity_config_on_a_non_agent_profile_is_rejected() -> None:
    """Settings that would be silently discarded fail at startup instead."""
    with pytest.raises(ValueError, match="not an Antigravity managed agent"):
        validate_antigravity_profile(
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
        validate_antigravity_profile(
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
        validate_antigravity_profile(
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
        validate_antigravity_profile(
            "misconfigured",
            ProcessingConfig(
                llm_model="antigravity-preview-05-2026",
                provider=provider,
            ),
            "antigravity-preview-05-2026",
        )
