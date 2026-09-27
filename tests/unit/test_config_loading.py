"""Tests for how resolve_service_profile layers a profile over the defaults.

The merge order is default_profile_settings, then the profile's section of
prompts.yaml, then the profile's own processing_config from config.yaml.
"""

import pytest

from family_assistant.config_loader import (
    PROFILE_SPECIALLY_HANDLED_PROCESSING_KEYS,
    resolve_service_profile,
)
from family_assistant.config_models import ProcessingConfig

PROFILE_REPLACEABLE_FIELDS = sorted(
    set(ProcessingConfig.model_fields) - PROFILE_SPECIALLY_HANDLED_PROCESSING_KEYS
)


class TestPromptsYamlServiceProfilesMerging:
    """Tests for merging the prompts.yaml service_profiles section."""

    def test_prompts_yaml_adds_keys_while_keeping_default_prompts(self) -> None:
        default_settings = {
            "processing_config": {
                "prompts": {
                    "system_prompt": "Default prompt",
                    "calendar_header": "Calendar events:",
                },
                "timezone": "UTC",
            },
        }
        prompts_yaml_service_profiles = {
            "my_profile": {"custom_prompt_key": "Custom value from prompts.yaml"},
        }

        resolved = resolve_service_profile(
            profile_def={"id": "my_profile"},
            default_settings=default_settings,
            prompts_yaml_service_profiles=prompts_yaml_service_profiles,
        )

        assert resolved["processing_config"]["prompts"] == {
            "system_prompt": "Default prompt",
            "calendar_header": "Calendar events:",
            "custom_prompt_key": "Custom value from prompts.yaml",
        }

    def test_profile_absent_from_prompts_yaml_keeps_default_prompts(self) -> None:
        default_settings = {
            "processing_config": {
                "prompts": {"system_prompt": "Default prompt"},
                "timezone": "UTC",
            },
        }
        prompts_yaml_service_profiles = {
            "other_profile": {"system_prompt": "Other profile prompt"},
        }

        resolved = resolve_service_profile(
            profile_def={"id": "my_profile"},
            default_settings=default_settings,
            prompts_yaml_service_profiles=prompts_yaml_service_profiles,
        )

        assert resolved["processing_config"]["prompts"] == {
            "system_prompt": "Default prompt"
        }


@pytest.mark.parametrize("field", PROFILE_REPLACEABLE_FIELDS)
def test_profile_value_replaces_inherited_processing_config_field(field: str) -> None:
    """Every ProcessingConfig field a profile sets reaches its resolved config.

    max_iterations once went missing from the copied keys, so profiles that
    raised it silently ran with the default iteration limit.
    """
    default_settings = {
        "processing_config": {"prompts": {}, field: f"inherited-{field}"},
    }
    profile_def = {
        "id": "custom_profile",
        "processing_config": {field: f"declared-{field}"},
    }

    resolved = resolve_service_profile(
        profile_def=profile_def,
        default_settings=default_settings,
        prompts_yaml_service_profiles={},
    )

    assert resolved["processing_config"][field] == f"declared-{field}"
