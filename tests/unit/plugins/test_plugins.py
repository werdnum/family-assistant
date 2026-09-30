"""The plugin seam: config, profile selection, runtime and tool registration."""

from collections.abc import Iterator
from pathlib import Path
from typing import get_args
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr, ValidationError

from family_assistant.config_loader import load_config
from family_assistant.config_models import AppConfig
from family_assistant.plugins.base import PluginProfileContext
from family_assistant.plugins.config import (
    PluginsConfig,
    migrate_legacy_home_assistant_settings,
    resolve_profile_plugins,
)
from family_assistant.plugins.home_assistant.config import HomeAssistantConfig
from family_assistant.plugins.home_assistant.context import (
    HomeAssistantContextProvider,
)
from family_assistant.plugins.home_assistant.instance import HomeAssistantInstance
from family_assistant.plugins.home_assistant.plugin import HOME_ASSISTANT_PLUGIN
from family_assistant.plugins.registry import PLUGINS, PLUGINS_BY_ID
from family_assistant.plugins.runtime import PluginRuntime
from family_assistant.tools import LOCAL_TOOL_METADATA_BY_NAME, TOOLS_DEFINITION
from family_assistant.tools.metadata import ToolTag, join_tool_registrations


def _ha(**overrides: object) -> HomeAssistantConfig:
    return HomeAssistantConfig.model_validate({
        "api_url": "http://ha.local",
        "token": "token",
        **overrides,
    })


def test_plugins_config_has_one_field_per_registered_plugin() -> None:
    """A registered plugin with no config field could never be configured."""
    assert set(PluginsConfig.model_fields) == set(PLUGINS_BY_ID)
    for plugin in PLUGINS:
        annotation = PluginsConfig.model_fields[plugin.id].annotation
        assert get_args(annotation) == (str, plugin.config_model)


def test_plugin_tools_join_the_tool_catalogue_with_their_metadata() -> None:
    names = {definition["function"]["name"] for definition in TOOLS_DEFINITION}
    for registration in HOME_ASSISTANT_PLUGIN.tools:
        assert registration.name in names
        assert LOCAL_TOOL_METADATA_BY_NAME[registration.name] == registration.metadata
    assert ToolTag.EXTERNAL_COMM in (
        LOCAL_TOOL_METADATA_BY_NAME["call_home_assistant_action"].tags
    )


def test_join_tool_registrations_refuses_a_duplicate_name() -> None:
    registration = HOME_ASSISTANT_PLUGIN.tools[0]
    with pytest.raises(ValueError, match="Duplicate"):
        join_tool_registrations([registration], [registration])


class TestProfileSelection:
    def test_default_instance_serves_a_profile_that_names_none(self) -> None:
        plugins = PluginsConfig(home_assistant={"default": _ha()})
        assert resolve_profile_plugins(plugins, {}) == {"home_assistant": "default"}

    def test_profile_can_choose_another_instance(self) -> None:
        plugins = PluginsConfig(home_assistant={"default": _ha(), "cabin": _ha()})
        assert resolve_profile_plugins(plugins, {"home_assistant": "cabin"}) == {
            "home_assistant": "cabin"
        }

    def test_profile_can_go_without(self) -> None:
        plugins = PluginsConfig(home_assistant={"default": _ha()})
        assert resolve_profile_plugins(plugins, {"home_assistant": None}) == {}

    def test_no_default_instance_means_nothing_unless_chosen(self) -> None:
        plugins = PluginsConfig(home_assistant={"cabin": _ha()})
        assert resolve_profile_plugins(plugins, {}) == {}

    def test_unknown_instance_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no configured instance 'cabin'"):
            resolve_profile_plugins(PluginsConfig(), {"home_assistant": "cabin"})

    def test_unknown_plugin_is_refused(self) -> None:
        with pytest.raises(ValueError, match="Unknown plugin 'trino'"):
            resolve_profile_plugins(PluginsConfig(), {"trino": "default"})

    def test_app_config_refuses_a_profile_naming_a_missing_instance(self) -> None:
        with pytest.raises(ValidationError, match="Profile 'p' plugins"):
            AppConfig.model_validate({
                "service_profiles": [{"id": "p", "plugins": {"home_assistant": "x"}}]
            })

    def test_a_profile_override_merges_over_inherited_choices(
        self, tmp_path: Path
    ) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
plugins:
  home_assistant:
    default: {api_url: "http://ha.local", token: "t"}
service_profiles:
  - id: with_ha
  - id: without_ha
    plugins: {home_assistant: null}
"""
        )
        config = load_config(
            defaults_file_path=str(tmp_path / "missing_defaults.yaml"),
            config_file_path=str(config_file),
            prompts_file_path=str(tmp_path / "missing_prompts.yaml"),
            load_dotenv_file=False,
        )
        profiles = {profile.id: profile for profile in config.service_profiles}
        assert resolve_profile_plugins(config.plugins, profiles["with_ha"].plugins) == {
            "home_assistant": "default"
        }
        assert (
            resolve_profile_plugins(config.plugins, profiles["without_ha"].plugins)
            == {}
        )


class TestHomeAssistantConfigSources:
    def test_environment_supplies_the_default_instance_connection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """How production runs: template in YAML, URL and token from the environment."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
plugins:
  home_assistant:
    default:
      context_template: "{{ states('sun.sun') }}"
"""
        )
        monkeypatch.setenv("HOMEASSISTANT_URL", "http://ha.local:8123")
        monkeypatch.setenv("HOMEASSISTANT_API_KEY", "env-token")
        config = load_config(
            defaults_file_path=str(tmp_path / "missing_defaults.yaml"),
            config_file_path=str(config_file),
            prompts_file_path=str(tmp_path / "missing_prompts.yaml"),
            load_dotenv_file=False,
        )
        default = config.plugins.home_assistant["default"]
        assert default.api_url == "http://ha.local:8123"
        assert default.token == SecretStr("env-token")
        assert default.context_template == "{{ states('sun.sun') }}"

    def test_legacy_default_profile_settings_move_to_the_default_instance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
default_profile_settings:
  processing_config:
    home_assistant_context_template: "legacy template"
    home_assistant_verify_ssl: false
"""
        )
        monkeypatch.setenv("HOMEASSISTANT_URL", "http://ha.local:8123")
        monkeypatch.setenv("HOMEASSISTANT_API_KEY", "env-token")
        config = load_config(
            defaults_file_path=str(tmp_path / "missing_defaults.yaml"),
            config_file_path=str(config_file),
            prompts_file_path=str(tmp_path / "missing_prompts.yaml"),
            load_dotenv_file=False,
        )
        default = config.plugins.home_assistant["default"]
        assert default.context_template == "legacy template"
        assert default.verify_ssl is False
        assert default.api_url == "http://ha.local:8123"

    def test_legacy_event_source_switch_moves_to_the_default_instance(self) -> None:
        data: dict[str, object] = {
            "event_system": {"sources": {"home_assistant": {"enabled": False}}}
        }
        migrate_legacy_home_assistant_settings(data)
        assert data == {
            "event_system": {"sources": {}},
            "plugins": {"home_assistant": {"default": {"events": False}}},
        }

    def test_new_location_wins_over_legacy(self) -> None:
        data: dict[str, object] = {
            "default_profile_settings": {
                "processing_config": {"home_assistant_context_template": "old"}
            },
            "plugins": {"home_assistant": {"default": {"context_template": "new"}}},
        }
        migrate_legacy_home_assistant_settings(data)
        assert data["plugins"] == {
            "home_assistant": {"default": {"context_template": "new"}}
        }
        assert data["default_profile_settings"] == {"processing_config": {}}

    @pytest.mark.parametrize("token", ["token", ""])
    def test_an_instance_without_a_url_is_left_out(self, token: str) -> None:
        """Deployment templates set HOMEASSISTANT_API_KEY alone, often empty."""
        runtime = PluginRuntime(
            PluginsConfig(
                home_assistant={"default": HomeAssistantConfig(token=SecretStr(token))}
            )
        )
        assert runtime.for_profile({}).get(HomeAssistantInstance) is None


class TestRuntime:
    @pytest.fixture(autouse=True)
    def _fake_client(self) -> Iterator[None]:
        with patch(
            "family_assistant.plugins.home_assistant.plugin.create_home_assistant_client",
            side_effect=lambda **_: MagicMock(),
        ):
            yield

    def test_profiles_share_one_instance(self) -> None:
        runtime = PluginRuntime(PluginsConfig(home_assistant={"default": _ha()}))
        first = runtime.for_profile({}).get(HomeAssistantInstance)
        second = runtime.for_profile({}).get(HomeAssistantInstance)
        assert first is not None
        assert first is second
        assert (
            runtime.for_profile({"home_assistant": None}).get(HomeAssistantInstance)
            is None
        )

    def test_context_provider_only_with_a_template(self) -> None:
        runtime = PluginRuntime(
            PluginsConfig(
                home_assistant={
                    "default": _ha(context_template="{{ 1 }}"),
                    "bare": _ha(),
                }
            )
        )
        profile = PluginProfileContext(
            profile_id="p", prompts={}, timezone=ZoneInfo("UTC")
        )
        with_template = runtime.for_profile({}).context_providers(profile)
        assert [type(provider) for provider in with_template] == [
            HomeAssistantContextProvider
        ]
        assert (
            runtime.for_profile({"home_assistant": "bare"}).context_providers(profile)
            == []
        )

    def test_two_instances_with_events_are_refused(self) -> None:
        runtime = PluginRuntime(
            PluginsConfig(home_assistant={"default": _ha(), "cabin": _ha()})
        )
        with pytest.raises(ValueError, match="enable events on only one"):
            runtime.event_sources()

    def test_one_event_source_across_instances(self) -> None:
        runtime = PluginRuntime(
            PluginsConfig(home_assistant={"default": _ha(), "cabin": _ha(events=False)})
        )
        assert list(runtime.event_sources()) == ["home_assistant"]
