"""What the shipped configuration actually turns Auto on for.

Stage three ships the classifier in shadow mode: it records what it would have
chosen while every turn still runs on the profile's configured tier. Shipping
`active` by accident would move real spend on the profile every household
member talks to, so the mode, the profile that opts in, and the range it may
choose from are pinned together here rather than each on its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from family_assistant.config_models import (
    ModelTierConfig,
    ProcessingConfig,
    RetryModelConfig,
    ServiceProfile,
)
from family_assistant.llm.model_routing import MODEL_ROUTING_PROMPT_KEY
from family_assistant.llm.model_tiers import validate_profile_model_tier
from tests.unit.conftest import shipped_profile

if TYPE_CHECKING:
    from family_assistant.config_models import AppConfig

_STANDARD_ONLY = {
    "standard": ModelTierConfig(
        chain=[RetryModelConfig(provider="google", model="gemini-3.8-flash")]
    )
}


def test_the_classifier_runs_but_decides_nothing_yet(shipped_config: AppConfig) -> None:
    assert shipped_config.model_routing.mode == "shadow"
    assert shipped_config.model_routing.classifier.model
    assert shipped_config.model_routing.timeout_seconds > 0
    assert shipped_config.model_routing.history_messages > 0


def test_the_assistant_is_the_profile_auto_is_evaluated_on(
    shipped_config: AppConfig,
) -> None:
    profile = shipped_profile(shipped_config, "default_assistant")

    assert profile.processing_config.model_selection == "auto"
    assert profile.processing_config.model_tier == "standard"
    assert profile.auto_model_tiers == ["standard", "deep"]
    assert profile.auto_routing_guidance


def test_the_range_auto_may_choose_from_excludes_the_strongest_tier(
    shipped_config: AppConfig,
) -> None:
    """`frontier` is a person deciding a request is worth it, never an inference.

    A classifier false positive there has asymmetric cost, which is why the
    automatic list is narrower than what a user may pick.
    """
    profile = shipped_profile(shipped_config, "default_assistant")

    assert "frontier" in (profile.allowed_model_tiers or [])
    assert "frontier" not in (profile.auto_model_tiers or [])


def test_complex_tasks_is_not_routed(shipped_config: AppConfig) -> None:
    """It exists to be the deep one; there is nothing for a classifier to weigh."""
    profile = shipped_profile(shipped_config, "complex_tasks")

    assert profile.processing_config.model_selection == "explicit"


def test_no_other_shipped_profile_opts_into_routing_yet(
    shipped_config: AppConfig,
) -> None:
    """Auto is enabled one profile at a time, after shadow evaluation."""
    routed = [
        profile.id
        for profile in shipped_config.service_profiles
        if profile.processing_config.model_selection == "auto"
    ]

    assert routed == ["default_assistant"]


def test_the_shipped_prompt_has_the_placeholders_the_router_fills(
    shipped_config: AppConfig,
) -> None:
    """Startup refuses to route without this prompt, so its shape is a contract."""
    prompt = shipped_config.default_profile_settings.processing_config.prompts[
        MODEL_ROUTING_PROMPT_KEY
    ]

    assert "{tiers}" in prompt
    assert "{guidance}" in prompt


def test_auto_without_an_automatic_tier_list_is_a_startup_error() -> None:
    """A classifier with nothing to return would spend a call to say so."""
    profile = ServiceProfile(
        id="assistant",
        processing_config=ProcessingConfig(
            model_tier="standard", model_selection="auto"
        ),
        allowed_model_tiers=["standard"],
        auto_model_tiers=[],
    )

    with pytest.raises(ValueError, match="auto_model_tiers is empty"):
        validate_profile_model_tier(profile, _STANDARD_ONLY)


def test_auto_without_a_default_tier_is_a_startup_error() -> None:
    """Auto falls back to the profile's configured tier, so it needs one."""
    profile = ServiceProfile(
        id="assistant",
        processing_config=ProcessingConfig(
            llm_model="claude-opus-5", model_selection="auto"
        ),
    )

    with pytest.raises(ValueError, match="without a model_tier"):
        validate_profile_model_tier(profile, _STANDARD_ONLY)


def test_auto_with_a_configured_range_is_accepted() -> None:
    profile = ServiceProfile(
        id="assistant",
        processing_config=ProcessingConfig(
            model_tier="standard", model_selection="auto"
        ),
        allowed_model_tiers=["standard"],
        auto_model_tiers=["standard"],
    )

    assert (
        validate_profile_model_tier(profile, _STANDARD_ONLY)
        is _STANDARD_ONLY["standard"]
    )
