"""Tests for the configuration loading module.

These tests verify the configuration loading hierarchy:
1. Code defaults (lowest priority)
2. config.yaml file
3. Environment variables
4. CLI arguments (highest priority - tested in integration tests)
"""

# ast-grep-ignore-block: no-dict-any - Test utilities for dynamically loaded config dicts

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest
import yaml
from pydantic import ValidationError

from family_assistant.config_loader import (
    ENV_VAR_MAPPINGS,
    apply_calendar_env_vars,
    apply_env_var_overrides,
    deep_merge_dicts,
    get_code_defaults,
    get_nested_value,
    load_config,
    load_yaml_file,
    merge_yaml_config,
    parse_env_value,
    resolve_all_service_profiles,
    resolve_service_profile,
    set_nested_value,
    validate_timezone,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestDeepMergeDicts:
    """Tests for deep_merge_dicts function."""

    def test_simple_merge(self) -> None:
        """Test merging two simple dicts."""
        base = {"a": 1, "b": 2}
        merge = {"b": 3, "c": 4}
        result = deep_merge_dicts(base, merge)
        assert result == {"a": 1, "b": 3, "c": 4}
        # Original should be unchanged
        assert base == {"a": 1, "b": 2}

    def test_nested_merge(self) -> None:
        """Test merging nested dicts."""
        base = {"a": {"x": 1, "y": 2}, "b": 3}
        merge = {"a": {"y": 20, "z": 30}}
        result = deep_merge_dicts(base, merge)
        assert result == {"a": {"x": 1, "y": 20, "z": 30}, "b": 3}

    def test_deep_nested_merge(self) -> None:
        """Test merging deeply nested dicts."""
        base = {"l1": {"l2": {"l3": {"a": 1, "b": 2}}}}
        merge = {"l1": {"l2": {"l3": {"b": 20, "c": 30}}}}
        result = deep_merge_dicts(base, merge)
        assert result == {"l1": {"l2": {"l3": {"a": 1, "b": 20, "c": 30}}}}

    def test_non_dict_replaces(self) -> None:
        """Test that non-dict values replace rather than merge."""
        base = {"a": {"x": 1}}
        merge = {"a": "replaced"}
        result = deep_merge_dicts(base, merge)
        assert result == {"a": "replaced"}

    def test_empty_dicts(self) -> None:
        """Test merging with empty dicts."""
        assert deep_merge_dicts({}, {"a": 1}) == {"a": 1}
        assert deep_merge_dicts({"a": 1}, {}) == {"a": 1}
        assert deep_merge_dicts({}, {}) == {}


class TestNestedValueHelpers:
    """Tests for get_nested_value and set_nested_value functions."""

    def test_set_simple_path(self) -> None:
        """Test setting a simple path."""
        data: dict[str, Any] = {}
        set_nested_value(data, "key", "value")
        assert data == {"key": "value"}

    def test_set_nested_path(self) -> None:
        """Test setting a nested path."""
        data: dict[str, Any] = {}
        set_nested_value(data, "a.b.c", "value")
        assert data == {"a": {"b": {"c": "value"}}}

    def test_set_nested_path_existing(self) -> None:
        """Test setting a nested path with existing structure."""
        data: dict[str, Any] = {"a": {"b": {"x": 1}}}
        set_nested_value(data, "a.b.c", "value")
        assert data == {"a": {"b": {"x": 1, "c": "value"}}}

    def test_get_simple_path(self) -> None:
        """Test getting a simple path."""
        data = {"key": "value"}
        assert get_nested_value(data, "key") == "value"

    def test_get_nested_path(self) -> None:
        """Test getting a nested path."""
        data = {"a": {"b": {"c": "value"}}}
        assert get_nested_value(data, "a.b.c") == "value"

    def test_get_missing_path(self) -> None:
        """Test getting a missing path returns default."""
        data = {"a": {"b": 1}}
        assert get_nested_value(data, "a.c", "default") == "default"
        assert get_nested_value(data, "x.y.z") is None


class TestParseEnvValue:
    """Tests for parse_env_value function."""

    def test_parse_string(self) -> None:
        """Test parsing string values."""
        assert parse_env_value("hello", str) == "hello"
        result = parse_env_value("", str)
        assert len(result) == 0 and isinstance(result, str)

    def test_parse_int(self) -> None:
        """Test parsing integer values."""
        assert parse_env_value("42", int) == 42
        assert parse_env_value("-10", int) == -10
        with pytest.raises(ValueError):
            parse_env_value("not-a-number", int)

    def test_parse_bool(self) -> None:
        """Test parsing boolean values."""
        for true_val in ["true", "True", "TRUE", "1", "yes", "Yes"]:
            assert parse_env_value(true_val, bool) is True
        for false_val in ["false", "False", "FALSE", "0", "no", "No", "anything"]:
            assert parse_env_value(false_val, bool) is False

    def test_parse_list_integers(self) -> None:
        """Test parsing list of integers."""
        assert parse_env_value("1,2,3", list) == [1, 2, 3]
        assert parse_env_value("1, 2, 3", list) == [1, 2, 3]  # With spaces
        assert parse_env_value("42", list) == [42]  # Single item

    def test_parse_list_strings(self) -> None:
        """Test parsing list of strings (when not all are ints)."""
        assert parse_env_value("a,b,c", list) == ["a", "b", "c"]
        assert parse_env_value("a, b, c", list) == ["a", "b", "c"]

    def test_parse_list_empty_items(self) -> None:
        """Test parsing list with empty items."""
        assert parse_env_value("1,,3", list) == [1, 3]
        assert parse_env_value(",1,2,", list) == [1, 2]

    def test_parse_dict(self) -> None:
        """Test parsing dict values (key:value format)."""
        result = parse_env_value("123:Alice,456:Bob", dict)
        assert result == {123: "Alice", 456: "Bob"}

    def test_parse_dict_with_spaces(self) -> None:
        """Test parsing dict with spaces."""
        result = parse_env_value("123: Alice , 456: Bob", dict)
        assert result == {123: "Alice", 456: "Bob"}


class TestLoadYamlFile:
    """Tests for load_yaml_file function."""

    def test_load_valid_yaml(self, tmp_path: Path) -> None:
        """Test loading a valid YAML file."""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text("key: value\nnested:\n  a: 1\n  b: 2")

        result = load_yaml_file(yaml_file)
        assert result == {"key": "value", "nested": {"a": 1, "b": 2}}

    def test_load_missing_file(self, tmp_path: Path) -> None:
        """Test loading a missing file returns empty dict."""
        result = load_yaml_file(tmp_path / "nonexistent.yaml")
        assert result == {}

    def test_load_invalid_yaml(self, tmp_path: Path) -> None:
        """Test loading invalid YAML returns empty dict."""
        yaml_file = tmp_path / "invalid.yaml"
        yaml_file.write_text("this: is: not: valid: yaml: {{}")

        result = load_yaml_file(yaml_file)
        assert result == {}

    def test_load_non_dict_yaml(self, tmp_path: Path) -> None:
        """Test loading YAML that's not a dict returns empty dict."""
        yaml_file = tmp_path / "list.yaml"
        yaml_file.write_text("- item1\n- item2")

        result = load_yaml_file(yaml_file)
        assert result == {}


class TestGetCodeDefaults:
    """Tests for get_code_defaults function."""

    def test_returns_dict(self) -> None:
        """Test that defaults are returned as a dict."""
        defaults = get_code_defaults()
        assert isinstance(defaults, dict)

    def test_has_required_keys(self) -> None:
        """Test that required keys are present."""
        defaults = get_code_defaults()
        required_keys = [
            "telegram_token",
            "model",
            "embedding_model",
            "database_url",
            "server_url",
            "default_service_profile_id",
            "service_profiles",
            "default_profile_settings",
        ]
        for key in required_keys:
            assert key in defaults, f"Missing required key: {key}"

    def test_default_profile_settings_structure(self) -> None:
        """Test default_profile_settings has expected structure."""
        defaults = get_code_defaults()
        dps = defaults["default_profile_settings"]
        assert "processing_config" in dps
        assert "tools_config" in dps
        assert "chat_id_to_name_map" in dps
        assert "slash_commands" in dps


class TestApplyEnvVarOverrides:
    """Tests for apply_env_var_overrides function."""

    def test_applies_simple_env_var(self) -> None:
        """Test applying a simple environment variable."""
        config: dict[str, Any] = {"model": "default-model"}
        with mock.patch.dict(os.environ, {"LLM_MODEL": "new-model"}, clear=False):
            apply_env_var_overrides(config)
        assert config["model"] == "new-model"

    def test_applies_nested_env_var(self) -> None:
        """Test applying a nested environment variable."""
        config: dict[str, Any] = {"pwa_config": {}}
        with mock.patch.dict(os.environ, {"VAPID_PUBLIC_KEY": "test-key"}, clear=False):
            apply_env_var_overrides(config)
        assert config["pwa_config"]["vapid_public_key"] == "test-key"

    def test_applies_int_env_var(self) -> None:
        """Test applying an integer environment variable."""
        config: dict[str, Any] = {"embedding_dimensions": 768}
        with mock.patch.dict(os.environ, {"EMBEDDING_DIMENSIONS": "1536"}, clear=False):
            apply_env_var_overrides(config)
        assert config["embedding_dimensions"] == 1536

    def test_applies_bool_env_var(self) -> None:
        """Test applying a boolean environment variable."""
        config: dict[str, Any] = {"litellm_debug": False}
        with mock.patch.dict(os.environ, {"LITELLM_DEBUG": "true"}, clear=False):
            apply_env_var_overrides(config)
        assert config["litellm_debug"] is True

    def test_applies_list_env_var(self) -> None:
        """Test applying a list environment variable."""
        config: dict[str, Any] = {"allowed_user_ids": []}
        with mock.patch.dict(
            os.environ, {"ALLOWED_USER_IDS": "123,456,789"}, clear=False
        ):
            apply_env_var_overrides(config)
        assert config["allowed_user_ids"] == [123, 456, 789]

    def test_allowed_chat_ids_alias(self) -> None:
        """Test ALLOWED_CHAT_IDS as alias for ALLOWED_USER_IDS."""
        config: dict[str, Any] = {"allowed_user_ids": []}
        with mock.patch.dict(os.environ, {"ALLOWED_CHAT_IDS": "111,222"}, clear=False):
            apply_env_var_overrides(config)
        assert config["allowed_user_ids"] == [111, 222]

    def test_unset_env_var_preserves_default(self) -> None:
        """Test that unset env vars don't change values."""
        config: dict[str, Any] = {"model": "original-model"}
        # Ensure LLM_MODEL is not set
        env = {k: v for k, v in os.environ.items() if k != "LLM_MODEL"}
        with mock.patch.dict(os.environ, env, clear=True):
            apply_env_var_overrides(config)
        assert config["model"] == "original-model"


class TestApplyCalendarEnvVars:
    """Tests for apply_calendar_env_vars function."""

    def test_applies_caldav_config(self) -> None:
        """Test applying CalDAV configuration from env."""
        config: dict[str, Any] = {"calendar_config": {}}
        env = {
            "CALDAV_USERNAME": "user",
            "CALDAV_PASSWORD": "pass",
            "CALDAV_CALENDAR_URLS": "https://cal1.example.com,https://cal2.example.com",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            apply_calendar_env_vars(config)

        assert config["calendar_config"]["caldav"]["username"] == "user"
        assert config["calendar_config"]["caldav"]["password"] == "pass"
        assert len(config["calendar_config"]["caldav"]["calendar_urls"]) == 2

    def test_applies_ical_config(self) -> None:
        """Test applying iCal configuration from env."""
        config: dict[str, Any] = {"calendar_config": {}}
        env = {"ICAL_URLS": "https://ical1.example.com,https://ical2.example.com"}
        with mock.patch.dict(os.environ, env, clear=False):
            apply_calendar_env_vars(config)

        assert len(config["calendar_config"]["ical"]["urls"]) == 2

    def test_preserves_duplicate_detection(self) -> None:
        """Test that duplicate_detection settings are preserved."""
        config: dict[str, Any] = {
            "calendar_config": {
                "duplicate_detection": {"enabled": True, "threshold": 0.5}
            }
        }
        env = {"ICAL_URLS": "https://ical.example.com"}
        with mock.patch.dict(os.environ, env, clear=False):
            apply_calendar_env_vars(config)

        assert config["calendar_config"]["duplicate_detection"]["enabled"] is True
        assert config["calendar_config"]["duplicate_detection"]["threshold"] == 0.5


class TestValidateTimezone:
    """Tests for validate_timezone function."""

    def test_valid_timezone(self) -> None:
        """Test that valid timezone is accepted."""
        config: dict[str, Any] = {
            "default_profile_settings": {
                "processing_config": {"timezone": "America/New_York"}
            }
        }
        validate_timezone(config)
        assert (
            config["default_profile_settings"]["processing_config"]["timezone"]
            == "America/New_York"
        )

    def test_invalid_timezone_defaults_to_utc(self) -> None:
        """Test that invalid timezone defaults to UTC."""
        config: dict[str, Any] = {
            "default_profile_settings": {
                "processing_config": {"timezone": "Invalid/Timezone"}
            }
        }
        validate_timezone(config)
        assert (
            config["default_profile_settings"]["processing_config"]["timezone"] == "UTC"
        )


class TestMergeYamlConfig:
    """Tests for merge_yaml_config function."""

    def test_merges_top_level_keys(self) -> None:
        """Test merging top-level keys."""
        base: dict[str, Any] = {"model": "default", "server_url": "http://localhost"}
        yaml_config = {"model": "new-model", "new_key": "new_value"}
        merge_yaml_config(base, yaml_config)
        assert base["model"] == "new-model"
        assert base["new_key"] == "new_value"
        assert base["server_url"] == "http://localhost"

    def test_deep_merges_default_profile_settings(self) -> None:
        """Test deep merging of default_profile_settings."""
        base: dict[str, Any] = {
            "default_profile_settings": {
                "processing_config": {"timezone": "UTC", "max_iterations": 5},
                "tools_config": {"confirm_tools": []},
            }
        }
        yaml_config = {
            "default_profile_settings": {
                "processing_config": {"timezone": "America/New_York"},
            }
        }
        merge_yaml_config(base, yaml_config)
        assert (
            base["default_profile_settings"]["processing_config"]["timezone"]
            == "America/New_York"
        )
        # max_iterations should be preserved
        assert (
            base["default_profile_settings"]["processing_config"]["max_iterations"] == 5
        )


class TestResolveServiceProfile:
    """Tests for resolve_service_profile function."""

    def test_merges_defaults(self) -> None:
        """Test that defaults are applied."""
        default_settings: dict[str, Any] = {
            "processing_config": {
                "prompts": {"system_prompt": "Default prompt"},
                "timezone": "UTC",
                "max_iterations": 10,
            },
            "tools_config": {},
            "chat_id_to_name_map": {},
            "slash_commands": [],
        }
        profile_def = {"id": "test_profile", "description": "Test"}
        result = resolve_service_profile(profile_def, default_settings, {})
        assert result["id"] == "test_profile"
        assert result["processing_config"]["timezone"] == "UTC"
        assert result["processing_config"]["max_iterations"] == 10

    def test_profile_overrides_defaults(self) -> None:
        """Test that profile-specific values override defaults."""
        default_settings: dict[str, Any] = {
            "processing_config": {"timezone": "UTC", "max_iterations": 10},
            "tools_config": {},
            "chat_id_to_name_map": {},
            "slash_commands": [],
        }
        profile_def = {
            "id": "test_profile",
            "processing_config": {"max_iterations": 50, "llm_model": "custom-model"},
        }
        result = resolve_service_profile(profile_def, default_settings, {})
        assert result["processing_config"]["max_iterations"] == 50
        assert result["processing_config"]["llm_model"] == "custom-model"
        # Timezone should still be from defaults
        assert result["processing_config"]["timezone"] == "UTC"

    def test_prompts_yaml_merged_before_config_yaml(self) -> None:
        """Test that prompts.yaml service_profiles are merged before config.yaml."""
        default_settings: dict[str, Any] = {
            "processing_config": {
                "prompts": {"system_prompt": "Default prompt"},
            },
            "tools_config": {},
            "chat_id_to_name_map": {},
            "slash_commands": [],
        }
        prompts_yaml_profiles = {"my_profile": {"system_prompt": "Prompts YAML prompt"}}
        profile_def = {"id": "my_profile"}

        result = resolve_service_profile(
            profile_def, default_settings, prompts_yaml_profiles
        )
        assert (
            result["processing_config"]["prompts"]["system_prompt"]
            == "Prompts YAML prompt"
        )

    def test_config_yaml_overrides_prompts_yaml(self) -> None:
        """Test that config.yaml prompts override prompts.yaml."""
        default_settings: dict[str, Any] = {
            "processing_config": {
                "prompts": {"system_prompt": "Default prompt"},
            },
            "tools_config": {},
            "chat_id_to_name_map": {},
            "slash_commands": [],
        }
        prompts_yaml_profiles = {"my_profile": {"system_prompt": "Prompts YAML prompt"}}
        profile_def = {
            "id": "my_profile",
            "processing_config": {"prompts": {"system_prompt": "Config YAML prompt"}},
        }

        result = resolve_service_profile(
            profile_def, default_settings, prompts_yaml_profiles
        )
        assert (
            result["processing_config"]["prompts"]["system_prompt"]
            == "Config YAML prompt"
        )

    def test_tools_config_replaced_entirely(self) -> None:
        """Test that tools_config is replaced, not merged."""
        default_settings: dict[str, Any] = {
            "processing_config": {},
            "tools_config": {
                "confirm_tools": ["tool1", "tool2"],
                "enable_local_tools": ["all"],
            },
            "chat_id_to_name_map": {},
            "slash_commands": [],
        }
        profile_def = {
            "id": "test_profile",
            "tools_config": {"enable_local_tools": ["specific_tool"]},
        }
        result = resolve_service_profile(profile_def, default_settings, {})
        # tools_config should be completely replaced
        assert result["tools_config"] == {"enable_local_tools": ["specific_tool"]}

    def test_slash_commands_replaced(self) -> None:
        """Test that slash_commands list is replaced, not merged."""
        default_settings: dict[str, Any] = {
            "processing_config": {},
            "tools_config": {},
            "chat_id_to_name_map": {},
            "slash_commands": ["/default"],
        }
        profile_def = {"id": "test_profile", "slash_commands": ["/custom1", "/custom2"]}
        result = resolve_service_profile(profile_def, default_settings, {})
        assert result["slash_commands"] == ["/custom1", "/custom2"]


class TestResolveAllServiceProfiles:
    """Tests for resolve_all_service_profiles function."""

    def test_creates_default_profile_when_none_defined(self) -> None:
        """Test that a default profile is created when none are defined."""
        config_data: dict[str, Any] = {
            "service_profiles": [],
            "default_service_profile_id": "default_assistant",
            "model": "test-model",
            "default_profile_settings": {
                "processing_config": {"timezone": "UTC"},
                "tools_config": {},
                "chat_id_to_name_map": {},
                "slash_commands": [],
            },
        }
        profiles = resolve_all_service_profiles(config_data, {})
        assert len(profiles) == 1
        assert profiles[0]["id"] == "default_assistant"

    def test_resolves_multiple_profiles(self) -> None:
        """Test resolving multiple profiles."""
        config_data: dict[str, Any] = {
            "service_profiles": [
                {"id": "profile1", "description": "First"},
                {"id": "profile2", "description": "Second"},
            ],
            "default_service_profile_id": "profile1",
            "model": "test-model",
            "default_profile_settings": {
                "processing_config": {"timezone": "UTC"},
                "tools_config": {},
                "chat_id_to_name_map": {},
                "slash_commands": [],
            },
        }
        profiles = resolve_all_service_profiles(config_data, {})
        assert len(profiles) == 2
        assert profiles[0]["id"] == "profile1"
        assert profiles[1]["id"] == "profile2"


class TestLoadConfig:
    """Integration tests for the complete load_config function."""

    def test_loads_defaults_only(self, tmp_path: Path) -> None:
        """Test loading with no config files."""
        config_file = tmp_path / "nonexistent.yaml"
        prompts_file = tmp_path / "nonexistent_prompts.yaml"

        # Clear relevant env vars
        env_to_clear = [m.env_var for m in ENV_VAR_MAPPINGS]
        env_to_clear.extend([
            "CALDAV_USERNAME",
            "CALDAV_PASSWORD",
            "CALDAV_CALENDAR_URLS",
            "ICAL_URLS",
            "MCP_CONFIG_PATH",
            "INDEXING_PIPELINE_CONFIG_JSON",
        ])
        clean_env = {k: v for k, v in os.environ.items() if k not in env_to_clear}

        with mock.patch.dict(os.environ, clean_env, clear=True):
            config = load_config(
                config_file_path=str(config_file),
                prompts_file_path=str(prompts_file),
                load_dotenv_file=False,
            )

        assert config.model == "gemini/gemini-2.5-pro"
        assert config.database_url == "sqlite+aiosqlite:///family_assistant.db"

    def test_yaml_overrides_defaults(self, tmp_path: Path) -> None:
        """Test that YAML config overrides defaults."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump({
                "model": "custom-model",
                "server_url": "http://custom.example.com",
            })
        )
        prompts_file = tmp_path / "prompts.yaml"
        prompts_file.write_text(yaml.dump({"system_prompt": "Custom prompt"}))

        env_to_clear = [m.env_var for m in ENV_VAR_MAPPINGS]
        env_to_clear.extend([
            "CALDAV_USERNAME",
            "CALDAV_PASSWORD",
            "CALDAV_CALENDAR_URLS",
            "ICAL_URLS",
            "MCP_CONFIG_PATH",
            "INDEXING_PIPELINE_CONFIG_JSON",
        ])
        clean_env = {k: v for k, v in os.environ.items() if k not in env_to_clear}

        with mock.patch.dict(os.environ, clean_env, clear=True):
            config = load_config(
                config_file_path=str(config_file),
                prompts_file_path=str(prompts_file),
                load_dotenv_file=False,
            )

        assert config.model == "custom-model"
        assert config.server_url == "http://custom.example.com"

    def test_env_vars_override_yaml(self, tmp_path: Path) -> None:
        """Test that environment variables override YAML config."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"model": "yaml-model"}))
        prompts_file = tmp_path / "prompts.yaml"
        prompts_file.write_text(yaml.dump({"system_prompt": "test"}))

        env_to_clear = [m.env_var for m in ENV_VAR_MAPPINGS]
        env_to_clear.extend([
            "CALDAV_USERNAME",
            "CALDAV_PASSWORD",
            "CALDAV_CALENDAR_URLS",
            "ICAL_URLS",
            "MCP_CONFIG_PATH",
            "INDEXING_PIPELINE_CONFIG_JSON",
        ])
        clean_env = {k: v for k, v in os.environ.items() if k not in env_to_clear}
        clean_env["LLM_MODEL"] = "env-model"

        with mock.patch.dict(os.environ, clean_env, clear=True):
            config = load_config(
                config_file_path=str(config_file),
                prompts_file_path=str(prompts_file),
                load_dotenv_file=False,
            )

        assert config.model == "env-model"

    def test_service_profiles_resolved(self, tmp_path: Path) -> None:
        """Test that service profiles are properly resolved."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump({
                "service_profiles": [
                    {
                        "id": "test_profile",
                        "description": "A test profile",
                        "processing_config": {"max_iterations": 25},
                    }
                ]
            })
        )
        prompts_file = tmp_path / "prompts.yaml"
        prompts_file.write_text(yaml.dump({"system_prompt": "test"}))

        env_to_clear = [m.env_var for m in ENV_VAR_MAPPINGS]
        env_to_clear.extend([
            "CALDAV_USERNAME",
            "CALDAV_PASSWORD",
            "CALDAV_CALENDAR_URLS",
            "ICAL_URLS",
            "MCP_CONFIG_PATH",
            "INDEXING_PIPELINE_CONFIG_JSON",
        ])
        clean_env = {k: v for k, v in os.environ.items() if k not in env_to_clear}

        with mock.patch.dict(os.environ, clean_env, clear=True):
            config = load_config(
                config_file_path=str(config_file),
                prompts_file_path=str(prompts_file),
                load_dotenv_file=False,
            )

        assert len(config.service_profiles) == 1
        assert config.service_profiles[0].id == "test_profile"
        assert config.service_profiles[0].processing_config.max_iterations == 25

    def test_invalid_config_raises_validation_error(self, tmp_path: Path) -> None:
        """Test that invalid config raises ValidationError."""
        config_file = tmp_path / "config.yaml"
        # Use an invalid key that Pydantic will reject
        config_file.write_text(yaml.dump({"invalid_key_that_does_not_exist": "value"}))
        prompts_file = tmp_path / "prompts.yaml"
        prompts_file.write_text(yaml.dump({"system_prompt": "test"}))

        env_to_clear = [m.env_var for m in ENV_VAR_MAPPINGS]
        env_to_clear.extend([
            "CALDAV_USERNAME",
            "CALDAV_PASSWORD",
            "CALDAV_CALENDAR_URLS",
            "ICAL_URLS",
            "MCP_CONFIG_PATH",
            "INDEXING_PIPELINE_CONFIG_JSON",
        ])
        clean_env = {k: v for k, v in os.environ.items() if k not in env_to_clear}

        with (
            mock.patch.dict(os.environ, clean_env, clear=True),
            pytest.raises(ValidationError),
        ):
            load_config(
                config_file_path=str(config_file),
                prompts_file_path=str(prompts_file),
                load_dotenv_file=False,
            )


class TestEnvVarMappingsComplete:
    """Tests to ensure environment variable mappings are complete and correct."""

    def test_all_mappings_have_valid_paths(self) -> None:
        """Test that all env var mappings point to valid config paths."""
        defaults = get_code_defaults()
        for mapping in ENV_VAR_MAPPINGS:
            # Each path should either exist in defaults or be a nested path
            # we can create (like pwa_config.vapid_public_key)
            parts = mapping.config_path.split(".")
            if len(parts) == 1:
                # Top-level key should exist in defaults
                assert parts[0] in defaults, (
                    f"Top-level key {parts[0]} not in defaults for {mapping.env_var}"
                )

    def test_all_secrets_are_mapped(self) -> None:
        """Test that known secret env vars have mappings."""
        secret_env_vars = [
            "TELEGRAM_BOT_TOKEN",
            "OPENROUTER_API_KEY",
            "GEMINI_API_KEY",
            "VAPID_PRIVATE_KEY",
        ]
        mapped_env_vars = {m.env_var for m in ENV_VAR_MAPPINGS}
        for secret in secret_env_vars:
            assert secret in mapped_env_vars, f"Secret {secret} not mapped"
