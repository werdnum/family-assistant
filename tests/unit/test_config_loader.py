"""Tests for the configuration loading module.

These tests verify the configuration loading hierarchy:
1. Pydantic model field defaults (lowest priority)
2. defaults.yaml file (shipped with app)
3. config.yaml file (operator-provided)
4. Environment variables
5. CLI arguments (highest priority - tested in integration tests)
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
    get_nested_value,
    load_config,
    parse_env_value,
    resolve_all_service_profiles,
    resolve_service_profile,
    set_nested_value,
    validate_timezone,
)
from family_assistant.config_models import AppConfig
from family_assistant.config_sources import (
    DeepMergedYamlSource,
    deep_merge_dicts,
    load_yaml_file,
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

    def test_dict_replaces_scalar(self) -> None:
        """Test that dict values replace scalar values."""
        base = {"a": "scalar"}
        merge = {"a": {"x": 1}}
        result = deep_merge_dicts(base, merge)
        assert result == {"a": {"x": 1}}

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

    def test_parse_dict_string_keys(self) -> None:
        """Test parsing dict with string keys (fallback when int conversion fails)."""
        result = parse_env_value("model_a:alias_a,model_b:alias_b", dict)
        assert result == {"model_a": "alias_a", "model_b": "alias_b"}

    def test_parse_dict_mixed_keys_uses_strings(self) -> None:
        """Test that mixed int/string keys all become strings."""
        result = parse_env_value("123:Alice,bob:Bob", dict)
        # Since "bob" can't be int, all keys become strings
        assert result == {"123": "Alice", "bob": "Bob"}


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


class TestDeepMergedYamlSource:
    """Tests for DeepMergedYamlSource."""

    def test_single_file(self, tmp_path: Path) -> None:
        """Test loading a single YAML file."""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(yaml.dump({"model": "test-model", "server_port": 9000}))

        source = DeepMergedYamlSource(AppConfig, [str(yaml_file)])
        result = source()
        assert result["model"] == "test-model"
        assert result["server_port"] == 9000

    def test_two_files_deep_merge(self, tmp_path: Path) -> None:
        """Test that two files are deep-merged (later overrides earlier)."""
        defaults = tmp_path / "defaults.yaml"
        defaults.write_text(
            yaml.dump({
                "model": "default-model",
                "gemini_live_config": {
                    "voice": {"name": "Puck"},
                    "vad": {"automatic": True},
                },
            })
        )
        config = tmp_path / "config.yaml"
        config.write_text(
            yaml.dump({
                "model": "operator-model",
                "gemini_live_config": {"voice": {"name": "Kore"}},
            })
        )

        source = DeepMergedYamlSource(AppConfig, [str(defaults), str(config)])
        result = source()
        assert result["model"] == "operator-model"
        assert result["gemini_live_config"]["voice"]["name"] == "Kore"
        # VAD settings from defaults should be preserved
        assert result["gemini_live_config"]["vad"]["automatic"] is True

    def test_missing_files_skipped(self, tmp_path: Path) -> None:
        """Test that missing files are silently skipped."""
        yaml_file = tmp_path / "exists.yaml"
        yaml_file.write_text(yaml.dump({"model": "test"}))

        source = DeepMergedYamlSource(
            AppConfig, [str(tmp_path / "missing.yaml"), str(yaml_file)]
        )
        result = source()
        assert result["model"] == "test"

    def test_empty_files_produce_empty_dict(self, tmp_path: Path) -> None:
        """Test that empty YAML files produce empty dict."""
        yaml_file = tmp_path / "empty.yaml"
        yaml_file.write_text("")

        source = DeepMergedYamlSource(AppConfig, [str(yaml_file)])
        assert source() == {}

    def test_no_files(self) -> None:
        """Test with no files at all."""
        source = DeepMergedYamlSource(AppConfig, [])
        assert source() == {}


class TestMcpConfigDeepMerge:
    """Regression tests for MCP config deep merging.

    This is the motivating use case: config.yaml should be able to add MCP
    servers without losing defaults from defaults.yaml.
    """

    def test_config_yaml_adds_mcp_servers_to_defaults(self, tmp_path: Path) -> None:
        """Adding MCP servers in config.yaml preserves defaults.yaml servers."""
        defaults = tmp_path / "defaults.yaml"
        defaults.write_text(
            yaml.dump({
                "mcp_config": {
                    "mcpServers": {
                        "time": {"command": "uvx", "args": ["mcp-server-time"]},
                        "browser": {"command": "deno", "args": ["run", "-A"]},
                        "brave": {
                            "command": "deno",
                            "args": ["run", "--allow-net"],
                        },
                    }
                }
            })
        )
        config = tmp_path / "config.yaml"
        config.write_text(
            yaml.dump({
                "mcp_config": {
                    "mcpServers": {
                        "custom": {"command": "my-server", "args": ["--port=8080"]},
                    }
                }
            })
        )

        source = DeepMergedYamlSource(AppConfig, [str(defaults), str(config)])
        result = source()
        servers = result["mcp_config"]["mcpServers"]
        assert "time" in servers
        assert "browser" in servers
        assert "brave" in servers
        assert "custom" in servers
        assert len(servers) == 4

    def test_config_yaml_overrides_existing_server_args(self, tmp_path: Path) -> None:
        """config.yaml can override an existing server's configuration."""
        defaults = tmp_path / "defaults.yaml"
        defaults.write_text(
            yaml.dump({
                "mcp_config": {
                    "mcpServers": {
                        "time": {
                            "command": "uvx",
                            "args": ["mcp-server-time", "--tz=UTC"],
                        },
                    }
                }
            })
        )
        config = tmp_path / "config.yaml"
        config.write_text(
            yaml.dump({
                "mcp_config": {
                    "mcpServers": {
                        "time": {
                            "command": "uvx",
                            "args": ["mcp-server-time", "--tz=US/Eastern"],
                        },
                    }
                }
            })
        )

        source = DeepMergedYamlSource(AppConfig, [str(defaults), str(config)])
        result = source()
        assert result["mcp_config"]["mcpServers"]["time"]["args"] == [
            "mcp-server-time",
            "--tz=US/Eastern",
        ]


class TestNestedConfigPartialOverride:
    """Tests for partial override of nested config via YAML layering."""

    def test_partial_override_preserves_other_fields(self, tmp_path: Path) -> None:
        """Overriding timezone in config.yaml preserves other processing_config fields."""
        defaults = tmp_path / "defaults.yaml"
        defaults.write_text(
            yaml.dump({
                "default_profile_settings": {
                    "processing_config": {
                        "timezone": "UTC",
                        "max_history_messages": 10,
                        "max_iterations": 5,
                    }
                }
            })
        )
        config = tmp_path / "config.yaml"
        config.write_text(
            yaml.dump({
                "default_profile_settings": {
                    "processing_config": {
                        "timezone": "America/New_York",
                    }
                }
            })
        )

        source = DeepMergedYamlSource(AppConfig, [str(defaults), str(config)])
        result = source()
        pc = result["default_profile_settings"]["processing_config"]
        assert pc["timezone"] == "America/New_York"
        assert pc["max_history_messages"] == 10
        assert pc["max_iterations"] == 5

    def test_gemini_voice_override_preserves_vad(self, tmp_path: Path) -> None:
        """Overriding gemini voice preserves VAD settings."""
        defaults = tmp_path / "defaults.yaml"
        defaults.write_text(
            yaml.dump({
                "gemini_live_config": {
                    "voice": {"name": "Puck"},
                    "vad": {"automatic": True, "silence_duration_ms": 500},
                }
            })
        )
        config = tmp_path / "config.yaml"
        config.write_text(
            yaml.dump({
                "gemini_live_config": {"voice": {"name": "Aoede"}},
            })
        )

        source = DeepMergedYamlSource(AppConfig, [str(defaults), str(config)])
        result = source()
        assert result["gemini_live_config"]["voice"]["name"] == "Aoede"
        assert result["gemini_live_config"]["vad"]["automatic"] is True
        assert result["gemini_live_config"]["vad"]["silence_duration_ms"] == 500


class TestAppConfigBackwardCompat:
    """Tests that existing AppConfig construction patterns still work."""

    def test_no_args_gives_field_defaults(self) -> None:
        """AppConfig() with no args produces field defaults only."""
        config = AppConfig()
        assert config.model == "gemini/gemini-2.5-pro"
        assert config.database_url == "sqlite+aiosqlite:///family_assistant.db"
        assert config.telegram_token is None

    def test_init_override(self) -> None:
        """AppConfig(field=value) overrides field defaults."""
        config = AppConfig(telegram_token="test-token")
        assert config.telegram_token == "test-token"
        assert config.model == "gemini/gemini-2.5-pro"

    def test_model_validate(self) -> None:
        """AppConfig.model_validate({...}) works as before."""
        config = AppConfig.model_validate({"model": "custom-model"})
        assert config.model == "custom-model"

    def test_model_copy_update(self) -> None:
        """config.model_copy(update={...}) works as before."""
        config = AppConfig()
        updated = config.model_copy(update={"server_port": 9999})
        assert updated.server_port == 9999
        assert config.server_port == 8000


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

    def test_retry_config_inherited_when_profile_has_none(self) -> None:
        """Test that retry_config from defaults is inherited when profile has None.

        This reproduces a bug where Pydantic model defaults (retry_config: None)
        would overwrite the inherited retry_config from default_profile_settings.
        """
        default_settings: dict[str, Any] = {
            "processing_config": {
                "timezone": "UTC",
                "retry_config": {
                    "primary": {
                        "provider": "google",
                        "model": "gemini-3.1-pro-preview",
                    },
                    "fallback": {"provider": "openai", "model": "gpt-5.2"},
                },
            },
            "tools_config": {},
            "chat_id_to_name_map": {},
            "slash_commands": [],
        }
        # Profile has retry_config: None (as Pydantic model defaults would produce)
        profile_def: dict[str, Any] = {
            "id": "test_profile",
            "description": "Test profile",
            "processing_config": {
                "retry_config": None,  # Pydantic default - should NOT override
                "provider": None,  # Pydantic default
                "llm_model": None,  # Pydantic default
            },
        }
        result = resolve_service_profile(profile_def, default_settings, {})

        # retry_config should be inherited from defaults, not overwritten with None
        assert result["processing_config"]["retry_config"] is not None
        assert (
            result["processing_config"]["retry_config"]["primary"]["provider"]
            == "google"
        )
        assert (
            result["processing_config"]["retry_config"]["primary"]["model"]
            == "gemini-3.1-pro-preview"
        )

    def test_retry_config_can_be_explicitly_overridden(self) -> None:
        """Test that profile can explicitly override retry_config."""
        default_settings: dict[str, Any] = {
            "processing_config": {
                "timezone": "UTC",
                "retry_config": {
                    "primary": {
                        "provider": "google",
                        "model": "gemini-3.1-pro-preview",
                    },
                    "fallback": {"provider": "openai", "model": "gpt-5.2"},
                },
            },
            "tools_config": {},
            "chat_id_to_name_map": {},
            "slash_commands": [],
        }
        # Profile explicitly sets a different retry_config
        profile_def: dict[str, Any] = {
            "id": "test_profile",
            "processing_config": {
                "retry_config": {
                    "primary": {"provider": "openai", "model": "gpt-4o"},
                },
            },
        }
        result = resolve_service_profile(profile_def, default_settings, {})

        # retry_config should be the profile's explicit value
        assert (
            result["processing_config"]["retry_config"]["primary"]["provider"]
            == "openai"
        )
        assert (
            result["processing_config"]["retry_config"]["primary"]["model"] == "gpt-4o"
        )


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

    def test_loads_field_defaults_only(self, tmp_path: Path) -> None:
        """Test loading with no config files uses pydantic field defaults."""
        defaults_file = tmp_path / "nonexistent_defaults.yaml"
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
                defaults_file_path=str(defaults_file),
                config_file_path=str(config_file),
                prompts_file_path=str(prompts_file),
                load_dotenv_file=False,
            )

        assert config.model == "gemini/gemini-2.5-pro"
        assert config.database_url == "sqlite+aiosqlite:///family_assistant.db"

    def test_defaults_yaml_overrides_field_defaults(self, tmp_path: Path) -> None:
        """Test that defaults.yaml overrides pydantic field defaults."""
        defaults_file = tmp_path / "defaults.yaml"
        defaults_file.write_text(
            yaml.dump({
                "model": "defaults-model",
                "server_url": "http://defaults.example.com",
            })
        )
        config_file = tmp_path / "nonexistent_config.yaml"
        prompts_file = tmp_path / "prompts.yaml"
        prompts_file.write_text(yaml.dump({"system_prompt": "Test prompt"}))

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
                defaults_file_path=str(defaults_file),
                config_file_path=str(config_file),
                prompts_file_path=str(prompts_file),
                load_dotenv_file=False,
            )

        assert config.model == "defaults-model"
        assert config.server_url == "http://defaults.example.com"

    def test_config_yaml_overrides_defaults_yaml(self, tmp_path: Path) -> None:
        """Test that config.yaml (operator) overrides defaults.yaml (shipped)."""
        defaults_file = tmp_path / "defaults.yaml"
        defaults_file.write_text(
            yaml.dump({
                "model": "defaults-model",
                "server_url": "http://defaults.example.com",
                "database_url": "sqlite:///defaults.db",
            })
        )
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump({
                "model": "operator-model",
                # server_url not specified - should use defaults.yaml value
            })
        )
        prompts_file = tmp_path / "prompts.yaml"
        prompts_file.write_text(yaml.dump({"system_prompt": "Test prompt"}))

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
                defaults_file_path=str(defaults_file),
                config_file_path=str(config_file),
                prompts_file_path=str(prompts_file),
                load_dotenv_file=False,
            )

        # config.yaml should override defaults.yaml for model
        assert config.model == "operator-model"
        # server_url should come from defaults.yaml (not overridden)
        assert config.server_url == "http://defaults.example.com"
        # database_url should come from defaults.yaml (not overridden)
        assert config.database_url == "sqlite:///defaults.db"

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

    def test_mcp_servers_deep_merged_across_yaml_files(self, tmp_path: Path) -> None:
        """Regression: config.yaml adding MCP servers doesn't lose defaults.yaml servers."""
        defaults_file = tmp_path / "defaults.yaml"
        defaults_file.write_text(
            yaml.dump({
                "mcp_config": {
                    "mcpServers": {
                        "time": {"command": "uvx", "args": ["mcp-server-time"]},
                        "brave": {"command": "deno", "args": ["run"]},
                    }
                }
            })
        )
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump({
                "mcp_config": {
                    "mcpServers": {
                        "custom": {"command": "my-server"},
                    }
                }
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
                defaults_file_path=str(defaults_file),
                config_file_path=str(config_file),
                prompts_file_path=str(prompts_file),
                load_dotenv_file=False,
            )

        servers = config.mcp_config.mcpServers
        assert "time" in servers
        assert "brave" in servers
        assert "custom" in servers


class TestEnvVarMappingsComplete:
    """Tests to ensure environment variable mappings are complete and correct."""

    def test_all_mappings_have_valid_paths(self) -> None:
        """Test that all env var mappings point to valid config paths."""
        config = AppConfig()
        defaults = config.model_dump()
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
