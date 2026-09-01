"""Tests for the deployment-effective local tool registry."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from family_assistant.config_loader import load_config
from family_assistant.services.effective_tool_registry import (
    build_effective_local_tool_definitions,
    build_effective_local_tool_registrations,
)
from family_assistant.services.oauth_integration_state import OAuthIntegrationState
from family_assistant.tools import (
    TOOLS_DEFINITION,
    LocalToolsProvider,
    build_local_tool_descriptors,
)
from family_assistant.tools.google_data import GOOGLE_TOOL_REQUIRED_SCOPES

if TYPE_CHECKING:
    from pathlib import Path

    from family_assistant.config_models import AppConfig
    from family_assistant.tools import ToolDefinition

pytestmark = pytest.mark.no_db


def _load_defaults(tmp_path: Path) -> AppConfig:
    return load_config(
        defaults_file_path="defaults.yaml",
        config_file_path=str(tmp_path / "missing-config.yaml"),
    )


def _definition_by_name(definitions: list[ToolDefinition], name: str) -> ToolDefinition:
    return next(
        definition
        for definition in definitions
        if definition.get("function", {}).get("name") == name
    )


def _google_state(enabled_tool_names: frozenset[str]) -> OAuthIntegrationState:
    return OAuthIntegrationState(
        provider="google",
        enabled=bool(enabled_tool_names),
        reason=None if enabled_tool_names else "Google integration is disabled.",
        taint_enforcement_waived=False,
        enabled_tool_names=enabled_tool_names,
        governed_tool_names=frozenset(GOOGLE_TOOL_REQUIRED_SCOPES),
    )


def test_deployment_config_customizes_worker_schema_without_mutating_source(
    tmp_path: Path,
) -> None:
    config = _load_defaults(tmp_path)
    config.ai_worker_config.available_agents = ["codex"]

    effective = build_effective_local_tool_definitions(config)
    effective_worker = _definition_by_name(effective, "spawn_worker")
    source_worker = _definition_by_name(list(TOOLS_DEFINITION), "spawn_worker")

    assert effective_worker["function"]["parameters"]["properties"]["agent"][  # type: ignore[index]
        "enum"
    ] == ["codex"]
    assert source_worker["function"]["parameters"]["properties"]["agent"][  # type: ignore[index]
        "enum"
    ] == ["claude", "gemini"]


def test_deployment_document_inventory_customizes_documentation_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _load_defaults(tmp_path)
    monkeypatch.setattr(
        "family_assistant.services.effective_tool_registry._scan_user_docs",
        lambda: ["calendar.md", "smart-home.md"],
    )

    definitions = build_effective_local_tool_definitions(config)
    documentation_tool = _definition_by_name(
        definitions, "get_user_documentation_content"
    )

    description = documentation_tool["function"]["description"]  # type: ignore[index]
    assert "calendar.md, smart-home.md" in description


def test_disabled_google_integration_removes_every_governed_tool(
    tmp_path: Path,
) -> None:
    config = _load_defaults(tmp_path)

    registrations = build_effective_local_tool_registrations(
        config, _google_state(frozenset())
    )

    assert {registration.name for registration in registrations}.isdisjoint(
        GOOGLE_TOOL_REQUIRED_SCOPES
    )


@pytest.mark.asyncio
async def test_scope_limited_registry_matches_local_provider_descriptors(
    tmp_path: Path,
) -> None:
    config = _load_defaults(tmp_path)
    enabled = frozenset({"gmail_search", "drive_search"})
    registrations = build_effective_local_tool_registrations(
        config, _google_state(enabled)
    )
    provider = LocalToolsProvider(registrations=registrations)

    descriptors = await provider.get_tool_descriptors()

    assert descriptors == build_local_tool_descriptors(registrations)
    descriptor_names = {descriptor.name for descriptor in descriptors}
    assert descriptor_names & set(GOOGLE_TOOL_REQUIRED_SCOPES) == enabled
