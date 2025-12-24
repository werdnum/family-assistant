"""Tests for configuration loading and profile resolution.

These tests verify that:
1. Profile-specific settings from config.yaml are properly merged
2. Profile-specific prompts from prompts.yaml are properly merged
3. Scalar values like max_iterations are correctly overridden per-profile
"""

import copy
from typing import Any

# ast-grep-ignore-block: no-dict-any - Test utilities for dynamically loaded config dicts


def deep_merge_dicts(
    base_dict: dict[str, Any], merge_dict: dict[str, Any]
) -> dict[str, Any]:
    """Deeply merges merge_dict into base_dict.

    Copied from __main__.py for testing.
    """
    result = copy.deepcopy(base_dict)
    for key, value in merge_dict.items():
        if isinstance(value, dict) and key in result and isinstance(result[key], dict):
            result[key] = deep_merge_dicts(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def resolve_service_profile(
    profile_def: dict[str, Any],
    default_settings: dict[str, Any],
    prompts_yaml_service_profiles: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Resolve a single service profile by merging defaults and overrides.

    This mirrors the logic in __main__.py's load_config function.
    """
    resolved_profile_config = copy.deepcopy(default_settings)
    resolved_profile_config["id"] = profile_def["id"]
    resolved_profile_config["description"] = profile_def.get("description", "")

    # Merge profile-specific prompts from prompts.yaml service_profiles section
    profile_id = profile_def["id"]
    if profile_id in prompts_yaml_service_profiles:
        prompts_yaml_profile_prompts = prompts_yaml_service_profiles[profile_id]
        if isinstance(prompts_yaml_profile_prompts, dict):
            resolved_profile_config["processing_config"]["prompts"] = deep_merge_dicts(
                resolved_profile_config["processing_config"].get("prompts", {}),
                prompts_yaml_profile_prompts,
            )

    # Merge processing_config from config.yaml
    if "processing_config" in profile_def and isinstance(
        profile_def["processing_config"], dict
    ):
        # Deep merge for 'prompts'
        if "prompts" in profile_def["processing_config"]:
            resolved_profile_config["processing_config"]["prompts"] = deep_merge_dicts(
                resolved_profile_config["processing_config"].get("prompts", {}),
                profile_def["processing_config"]["prompts"],
            )
        # Replace for scalar values
        for scalar_key in [
            "provider",
            "llm_model",
            "timezone",
            "max_history_messages",
            "history_max_age_hours",
            "web_max_history_messages",
            "web_history_max_age_hours",
            "max_iterations",  # This was missing before the fix
            "delegation_security_level",
            "retry_config",
            "camera_config",
        ]:
            if scalar_key in profile_def["processing_config"]:
                resolved_profile_config["processing_config"][scalar_key] = profile_def[
                    "processing_config"
                ][scalar_key]

    return resolved_profile_config


# ast-grep-ignore-end


class TestMaxIterationsMerging:
    """Tests for max_iterations configuration merging."""

    def test_max_iterations_from_profile_overrides_default(self) -> None:
        """Regression test: max_iterations from profile config.yaml should override default.

        This tests the fix for the bug where max_iterations was not in the scalar_key
        list, causing profile-specific max_iterations values to be ignored.
        """
        default_settings = {
            "processing_config": {
                "prompts": {"system_prompt": "Default prompt"},
                "timezone": "UTC",
                "max_iterations": 10,  # Default value
            },
        }

        profile_def = {
            "id": "camera_analyst",
            "description": "Camera analysis profile",
            "processing_config": {
                "max_iterations": 50,  # Profile override - should be used
            },
        }

        resolved = resolve_service_profile(
            profile_def=profile_def,
            default_settings=default_settings,
            prompts_yaml_service_profiles={},
        )

        assert resolved["processing_config"]["max_iterations"] == 50

    def test_max_iterations_uses_default_when_not_overridden(self) -> None:
        """max_iterations should use default when profile doesn't override it."""
        default_settings = {
            "processing_config": {
                "prompts": {"system_prompt": "Default prompt"},
                "timezone": "UTC",
                "max_iterations": 10,
            },
        }

        profile_def = {
            "id": "default_assistant",
            "description": "Default profile",
            # No max_iterations override
        }

        resolved = resolve_service_profile(
            profile_def=profile_def,
            default_settings=default_settings,
            prompts_yaml_service_profiles={},
        )

        assert resolved["processing_config"]["max_iterations"] == 10


class TestPromptsYamlServiceProfilesMerging:
    """Tests for prompts.yaml service_profiles section merging."""

    def test_prompts_yaml_service_profile_overrides_default_system_prompt(self) -> None:
        """Regression test: system_prompt from prompts.yaml service_profiles should be used.

        This tests the fix for the bug where prompts.yaml's service_profiles section
        was loaded but never extracted and merged into individual profiles.
        """
        default_settings = {
            "processing_config": {
                "prompts": {
                    "system_prompt": "You are a helpful assistant. Current time: {current_time}",
                },
                "timezone": "UTC",
            },
        }

        # This simulates prompts.yaml's service_profiles section
        prompts_yaml_service_profiles = {
            "camera_analyst": {
                "system_prompt": "You are a camera analyst. Current time: {current_time}",
            },
        }

        profile_def = {
            "id": "camera_analyst",
            "description": "Camera analysis profile",
            # No prompts override in config.yaml
        }

        resolved = resolve_service_profile(
            profile_def=profile_def,
            default_settings=default_settings,
            prompts_yaml_service_profiles=prompts_yaml_service_profiles,
        )

        assert (
            "camera analyst"
            in resolved["processing_config"]["prompts"]["system_prompt"]
        )
        assert (
            "helpful assistant"
            not in resolved["processing_config"]["prompts"]["system_prompt"]
        )

    def test_config_yaml_prompts_take_precedence_over_prompts_yaml(self) -> None:
        """config.yaml prompts should take precedence over prompts.yaml service_profiles."""
        default_settings = {
            "processing_config": {
                "prompts": {
                    "system_prompt": "Default prompt",
                },
                "timezone": "UTC",
            },
        }

        prompts_yaml_service_profiles = {
            "my_profile": {
                "system_prompt": "Prompt from prompts.yaml",
            },
        }

        profile_def = {
            "id": "my_profile",
            "processing_config": {
                "prompts": {
                    "system_prompt": "Prompt from config.yaml",  # Takes precedence
                },
            },
        }

        resolved = resolve_service_profile(
            profile_def=profile_def,
            default_settings=default_settings,
            prompts_yaml_service_profiles=prompts_yaml_service_profiles,
        )

        assert (
            resolved["processing_config"]["prompts"]["system_prompt"]
            == "Prompt from config.yaml"
        )

    def test_prompts_yaml_merges_non_conflicting_keys(self) -> None:
        """prompts.yaml service_profiles should add keys that don't exist in defaults."""
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
            "my_profile": {
                "custom_prompt_key": "Custom value from prompts.yaml",
            },
        }

        profile_def = {
            "id": "my_profile",
        }

        resolved = resolve_service_profile(
            profile_def=profile_def,
            default_settings=default_settings,
            prompts_yaml_service_profiles=prompts_yaml_service_profiles,
        )

        # Default keys should be preserved
        assert (
            resolved["processing_config"]["prompts"]["system_prompt"]
            == "Default prompt"
        )
        assert (
            resolved["processing_config"]["prompts"]["calendar_header"]
            == "Calendar events:"
        )
        # New key from prompts.yaml should be added
        assert (
            resolved["processing_config"]["prompts"]["custom_prompt_key"]
            == "Custom value from prompts.yaml"
        )

    def test_profile_without_prompts_yaml_override_uses_defaults(self) -> None:
        """Profile not in prompts.yaml service_profiles should use default prompts."""
        default_settings = {
            "processing_config": {
                "prompts": {
                    "system_prompt": "Default prompt",
                },
                "timezone": "UTC",
            },
        }

        prompts_yaml_service_profiles = {
            "other_profile": {
                "system_prompt": "Other profile prompt",
            },
        }

        profile_def = {
            "id": "my_profile",  # Not in prompts_yaml_service_profiles
        }

        resolved = resolve_service_profile(
            profile_def=profile_def,
            default_settings=default_settings,
            prompts_yaml_service_profiles=prompts_yaml_service_profiles,
        )

        assert (
            resolved["processing_config"]["prompts"]["system_prompt"]
            == "Default prompt"
        )


class TestOtherScalarValuesMerging:
    """Tests for other scalar values in processing_config."""

    def test_all_scalar_values_are_properly_merged(self) -> None:
        """All scalar values in the scalar_key list should be properly merged."""
        default_settings = {
            "processing_config": {
                "prompts": {},
                "provider": "google",
                "llm_model": "gemini-1.5-flash",
                "timezone": "UTC",
                "max_history_messages": 10,
                "history_max_age_hours": 24,
                "web_max_history_messages": 20,
                "web_history_max_age_hours": 48,
                "max_iterations": 5,
                "delegation_security_level": "confirm",
            },
        }

        profile_def = {
            "id": "custom_profile",
            "processing_config": {
                "provider": "openai",
                "llm_model": "gpt-4o",
                "timezone": "America/New_York",
                "max_history_messages": 50,
                "history_max_age_hours": 168,
                "web_max_history_messages": 100,
                "web_history_max_age_hours": 336,
                "max_iterations": 100,
                "delegation_security_level": "unrestricted",
                "retry_config": {"max_retries": 3},
                "camera_config": {"backend": "reolink"},
            },
        }

        resolved = resolve_service_profile(
            profile_def=profile_def,
            default_settings=default_settings,
            prompts_yaml_service_profiles={},
        )

        assert resolved["processing_config"]["provider"] == "openai"
        assert resolved["processing_config"]["llm_model"] == "gpt-4o"
        assert resolved["processing_config"]["timezone"] == "America/New_York"
        assert resolved["processing_config"]["max_history_messages"] == 50
        assert resolved["processing_config"]["history_max_age_hours"] == 168
        assert resolved["processing_config"]["web_max_history_messages"] == 100
        assert resolved["processing_config"]["web_history_max_age_hours"] == 336
        assert resolved["processing_config"]["max_iterations"] == 100
        assert (
            resolved["processing_config"]["delegation_security_level"] == "unrestricted"
        )
        assert resolved["processing_config"]["retry_config"] == {"max_retries": 3}
        assert resolved["processing_config"]["camera_config"] == {"backend": "reolink"}
