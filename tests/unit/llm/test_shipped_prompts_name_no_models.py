"""No shipped profile may tell the model which model it is.

A profile describes how an agent operates; a tier says how inference runs. Once
a tier is selectable per request, a prompt naming a model is wrong the moment
the tier resolves to anything else -- including the fallback of its own chain,
which is not a hypothetical: `complex_tasks` claimed to be Sol while falling
back to Fable long before selection existed.

This is a conformance test rather than a review habit because the failure is
silent: a stale model name reads as ordinary prose and the model repeats it to
the user as fact. The model ids come from the shipped configuration, so adding
a model to a tier extends what this refuses without anyone remembering to.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from family_assistant.llm.model_tiers import models_in_chain

if TYPE_CHECKING:
    from family_assistant.config_models import AppConfig

# Profile ids allowed to name their runtime. Empty on purpose: a profile whose
# capability genuinely depends on a provider says so by naming the *provider*
# ("only Gemini reads audio"), which is a durable fact, rather than a model id,
# which is not. Adding an entry needs a reason that survives the next model.
_ALLOWED_TO_NAME_ITS_MODEL: frozenset[str] = frozenset()


def _configured_model_ids(config: AppConfig) -> set[str]:
    """Every model id the shipped configuration names anywhere."""
    models: set[str] = set()
    for tier in config.model_tiers.values():
        models.update(models_in_chain(tier.chain, lambda _model: True))
    for profile in config.service_profiles:
        processing = profile.processing_config
        if processing.llm_model:
            models.add(processing.llm_model)
        retry = processing.retry_config
        if retry is not None:
            entries = [entry for entry in (retry.primary, retry.fallback) if entry]
            models.update(models_in_chain(entries, lambda _model: True))
        if processing.antigravity_config is not None:
            models.add(processing.antigravity_config.model)
    return {model for model in models if model}


def test_no_shipped_profile_prompt_or_description_names_a_model(
    shipped_config: AppConfig,
) -> None:
    model_ids = _configured_model_ids(shipped_config)
    assert model_ids, "the shipped configuration should name some models"

    offences: list[str] = []
    for profile in shipped_config.service_profiles:
        if profile.id in _ALLOWED_TO_NAME_ITS_MODEL:
            continue
        texts = {
            "description": profile.description or "",
            "system_prompt": profile.processing_config.prompts.get("system_prompt", ""),
        }
        offences.extend(
            f"{profile.id}.{where} names {model!r}"
            for where, text in texts.items()
            for model in model_ids
            if model.lower() in text.lower()
        )

    assert not offences, (
        "A profile's prompt or description must not name the model it runs on -- "
        "the tier decides that, and the actual model surfaces from runtime "
        "metadata instead. Offending text: " + "; ".join(sorted(offences))
    )


@pytest.mark.parametrize("profile_id", ["default_assistant", "complex_tasks"])
def test_the_tiered_profiles_are_covered_by_the_scan(
    shipped_config: AppConfig, profile_id: str
) -> None:
    """The scan is worthless if it silently stops seeing the profiles it guards."""
    assert any(
        profile.id == profile_id for profile in shipped_config.service_profiles
    ), f"{profile_id} is no longer shipped; update this test's expectations"
