from __future__ import annotations

from pathlib import Path

import yaml

from family_assistant.config_loader import (
    load_prompts_yaml,
    resolve_all_service_profiles,
)
from family_assistant.config_models import (
    DefaultProfileSettings,
    ServiceProfile,
)
from family_assistant.tools import LOCAL_TOOL_DESCRIPTORS, PolicyEngine, ToolDescriptor
from family_assistant.tools.policy import ToolPolicyDecision

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULTS_PATH = REPO_ROOT / "defaults.yaml"
PROMPTS_PATH = REPO_ROOT / "prompts.yaml"
MCP_SERVER_IDS = (
    "time",
    "brave",
    "homeassistant",
    "google-maps",
    "scrape",
    "browser",
)


def _load_defaults_yaml() -> dict[str, object]:
    loaded = yaml.safe_load(DEFAULTS_PATH.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _load_resolved_profiles() -> tuple[DefaultProfileSettings, list[ServiceProfile]]:
    config_data = _load_defaults_yaml()
    config_data.setdefault("default_service_profile_id", "default_assistant")
    default_profile_settings = config_data["default_profile_settings"]
    assert isinstance(default_profile_settings, dict)
    processing_config = default_profile_settings["processing_config"]
    assert isinstance(processing_config, dict)
    default_prompts, service_profile_prompts = load_prompts_yaml(str(PROMPTS_PATH))
    if default_prompts:
        processing_config["prompts"] = default_prompts
    config_data["service_profiles"] = resolve_all_service_profiles(
        config_data, service_profile_prompts
    )
    default_settings = DefaultProfileSettings.model_validate(default_profile_settings)
    profiles = [
        ServiceProfile.model_validate(profile)
        for profile in config_data["service_profiles"]
    ]
    return default_settings, profiles


def _make_mcp_descriptor(server_id: str) -> ToolDescriptor:
    tool_name = f"{server_id.replace('-', '_')}_tool"
    return ToolDescriptor(
        name=tool_name,
        definition={
            "type": "function",
            "function": {
                "name": tool_name,
                "description": f"Tool from {server_id}",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        tags=frozenset(),
        origin="mcp",
        mcp_server_id=server_id,
    )


def test_defaults_yaml_uses_policy_only_for_tool_access() -> None:
    defaults_data = _load_defaults_yaml()
    default_profile_settings = defaults_data["default_profile_settings"]
    assert isinstance(default_profile_settings, dict)
    assert "tools_policy" in default_profile_settings
    default_tools_config = default_profile_settings.get("tools_config", {})
    assert isinstance(default_tools_config, dict)
    assert "enable_local_tools" not in default_tools_config
    assert "enable_mcp_server_ids" not in default_tools_config
    assert "confirm_tools" not in default_tools_config

    service_profiles = defaults_data["service_profiles"]
    assert isinstance(service_profiles, list)

    for profile in service_profiles:
        assert isinstance(profile, dict)
        tools_config = profile.get("tools_config", {})
        assert isinstance(tools_config, dict)
        assert "enable_local_tools" not in tools_config
        assert "enable_mcp_server_ids" not in tools_config
        assert "confirm_tools" not in tools_config


def test_shipped_profiles_define_effective_tool_policy() -> None:
    default_settings, profiles = _load_resolved_profiles()
    descriptors = [*LOCAL_TOOL_DESCRIPTORS]
    descriptors.extend(_make_mcp_descriptor(server_id) for server_id in MCP_SERVER_IDS)

    profile_map: dict[str, DefaultProfileSettings | ServiceProfile] = {
        "default_profile_settings": default_settings,
        **{profile.id: profile for profile in profiles},
    }

    for profile_id, profile in profile_map.items():
        assert profile.tools_policy is not None, profile_id
        engine = PolicyEngine.from_policy_config(profile.tools_policy)

        for descriptor in descriptors:
            advertised_without_confirmation = engine.evaluate_for_advertisement(
                descriptor,
                can_confirm=False,
            )
            executable_without_confirmation = engine.evaluate_for_execution(
                descriptor,
                can_confirm=False,
            )

            assert isinstance(
                advertised_without_confirmation.decision,
                ToolPolicyDecision,
            )
            assert isinstance(
                executable_without_confirmation.decision,
                ToolPolicyDecision,
            )


def test_ucp_tools_are_default_on_demand_and_not_confirm_gated() -> None:
    default_settings, profiles = _load_resolved_profiles()
    shopping_tool_names = {
        "ucp_add_to_cart",
        "ucp_get_cart",
        "ucp_transfer_checkout_to_human",
    }
    descriptors = {
        descriptor.name: descriptor
        for descriptor in LOCAL_TOOL_DESCRIPTORS
        if descriptor.name in shopping_tool_names
    }
    assert set(descriptors) == shopping_tool_names

    default_engine = PolicyEngine.from_policy_config(default_settings.tools_policy)
    assert shopping_tool_names <= set(
        default_settings.tools_config.on_demand_local_tools
    )
    profile_by_id = {profile.id: profile for profile in profiles}
    assert "shopping_profile" not in profile_by_id
    browser_profile = profile_by_id["browser_profile"]
    browser_engine = PolicyEngine.from_policy_config(browser_profile.tools_policy)
    complex_tasks_profile = profile_by_id["complex_tasks"]
    complex_tasks_engine = PolicyEngine.from_policy_config(
        complex_tasks_profile.tools_policy
    )
    assert shopping_tool_names <= set(
        complex_tasks_profile.tools_config.on_demand_local_tools
    )

    for descriptor in descriptors.values():
        assert (
            default_engine.evaluate_for_advertisement(
                descriptor,
                can_confirm=True,
            ).decision
            == ToolPolicyDecision.ALLOW
        )
        assert (
            browser_engine.evaluate_for_advertisement(
                descriptor,
                can_confirm=False,
            ).decision
            == ToolPolicyDecision.ALLOW
        )
        assert (
            default_engine.evaluate_for_advertisement(
                descriptor,
                can_confirm=False,
            ).decision
            == ToolPolicyDecision.ALLOW
        )
        assert (
            complex_tasks_engine.evaluate_for_advertisement(
                descriptor,
                can_confirm=False,
            ).decision
            == ToolPolicyDecision.ALLOW
        )
