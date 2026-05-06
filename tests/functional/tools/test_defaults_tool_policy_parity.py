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
    ToolsConfig,
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
NON_LEGACY_POLICY_PROFILES = {"email_intake"}


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


def _legacy_decision(
    descriptor: ToolDescriptor,
    tools_config: ToolsConfig,
    *,
    can_confirm: bool,
) -> ToolPolicyDecision:
    if descriptor.origin == "local":
        all_local = tools_config.get_all_tool_names()
        enabled = all_local is None or descriptor.name in all_local
    else:
        all_mcp = tools_config.get_all_mcp_server_ids()
        enabled = all_mcp is None or descriptor.mcp_server_id in set(all_mcp)

    if not enabled:
        return ToolPolicyDecision.DENY

    if descriptor.name in set(tools_config.confirm_tools):
        return ToolPolicyDecision.CONFIRM if can_confirm else ToolPolicyDecision.DENY

    return ToolPolicyDecision.ALLOW


def test_defaults_yaml_dual_authors_tools_config_and_policy() -> None:
    defaults_data = _load_defaults_yaml()
    default_profile_settings = defaults_data["default_profile_settings"]
    assert isinstance(default_profile_settings, dict)
    assert "tools_config" in default_profile_settings
    assert "tools_policy" in default_profile_settings

    service_profiles = defaults_data["service_profiles"]
    assert isinstance(service_profiles, list)

    for profile in service_profiles:
        assert isinstance(profile, dict)
        if "tools_config" not in profile:
            continue
        assert "tools_policy" in profile, profile["id"]


def test_tools_policy_matches_legacy_defaults_for_shipped_profiles() -> None:
    default_settings, profiles = _load_resolved_profiles()
    descriptors = [*LOCAL_TOOL_DESCRIPTORS]
    descriptors.extend(_make_mcp_descriptor(server_id) for server_id in MCP_SERVER_IDS)

    profile_map: dict[str, DefaultProfileSettings | ServiceProfile] = {
        "default_profile_settings": default_settings,
        **{profile.id: profile for profile in profiles},
    }

    for profile_id, profile in profile_map.items():
        if profile_id in NON_LEGACY_POLICY_PROFILES:
            continue
        assert profile.tools_policy is not None, profile_id
        engine = PolicyEngine.from_policy_config(profile.tools_policy)

        for descriptor in descriptors:
            for can_confirm in (False, True):
                expected = _legacy_decision(
                    descriptor,
                    profile.tools_config,
                    can_confirm=can_confirm,
                )
                advertised = engine.evaluate_for_advertisement(
                    descriptor,
                    can_confirm=can_confirm,
                )
                executable = engine.evaluate_for_execution(
                    descriptor,
                    can_confirm=can_confirm,
                )

                assert advertised.decision is expected, (
                    profile_id,
                    descriptor.name,
                    descriptor.origin,
                    descriptor.mcp_server_id,
                    can_confirm,
                    "advertisement",
                )
                assert executable.decision is expected, (
                    profile_id,
                    descriptor.name,
                    descriptor.origin,
                    descriptor.mcp_server_id,
                    can_confirm,
                    "execution",
                )
