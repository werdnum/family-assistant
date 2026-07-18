"""Conformance tests for Google tool advertisement under the startup gate.

Ties together the two halves of the feature's gating:

- The shipped ``defaults.yaml`` profile policy: the five Google tools are
  policy-allowed in ``default_assistant`` and denied in the ambient
  ``email_intake`` / ``reminder`` profiles.
- The startup registration filter (``filter_google_tool_definitions``): when the
  integration is disabled the tools are dropped from the shared root
  definitions, so they are advertised in NO profile — including
  ``default_assistant`` — and when enabled only the scope-enabled subset survives.

The filter operates on the shared root definitions, so a profile can only ever
advertise a Google tool that is BOTH scope-enabled at startup AND allowed by that
profile's policy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from family_assistant.config_loader import load_config
from family_assistant.services.google_provider import GoogleScope
from family_assistant.services.oauth_integration_state import (
    OAuthIntegrationState,
    filter_oauth_tool_registrations,
)
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS as local_tool_implementations,
)
from family_assistant.tools import (
    LOCAL_TOOL_DESCRIPTORS,
    TOOLS_DEFINITION,
    LocalToolsProvider,
    PolicyEnforcingToolsProvider,
    PolicyEngine,
    ToolPolicyDecision,
    build_local_tool_registrations,
)
from family_assistant.tools import (
    LOCAL_TOOL_METADATA_BY_NAME as local_tool_metadata_by_name,
)
from family_assistant.tools.google_data import GOOGLE_TOOL_REQUIRED_SCOPES

if TYPE_CHECKING:
    from pathlib import Path

    from family_assistant.config_models import AppConfig
    from family_assistant.tools.metadata import ToolDescriptor

GOOGLE_TOOL_NAMES = frozenset({
    "gmail_search",
    "gmail_get_message",
    "gmail_get_attachment",
    "drive_search",
    "drive_get_file",
})


def _load_defaults(tmp_path: Path) -> AppConfig:
    return load_config(
        defaults_file_path="defaults.yaml",
        config_file_path=str(tmp_path / "missing-config.yaml"),
    )


def _profile_engine(config: AppConfig, profile_id: str) -> PolicyEngine:
    profile = next(p for p in config.service_profiles if p.id == profile_id)
    assert profile.tools_policy is not None
    return PolicyEngine.from_policy_config(profile.tools_policy)


def _descriptor_by_name() -> dict[str, ToolDescriptor]:
    return {descriptor.name: descriptor for descriptor in LOCAL_TOOL_DESCRIPTORS}


def _enabled_state(tool_names: frozenset[str]) -> OAuthIntegrationState:
    return OAuthIntegrationState(
        provider="google",
        enabled=True,
        reason=None,
        taint_enforcement_waived=False,
        enabled_tool_names=tool_names,
        governed_tool_names=frozenset(GOOGLE_TOOL_REQUIRED_SCOPES),
    )


def _disabled_state() -> OAuthIntegrationState:
    return OAuthIntegrationState(
        provider="google",
        enabled=False,
        reason="Google integration is not configured.",
        taint_enforcement_waived=False,
        enabled_tool_names=frozenset(),
        governed_tool_names=frozenset(GOOGLE_TOOL_REQUIRED_SCOPES),
    )


async def _advertised_names(
    state: OAuthIntegrationState,
    engine: PolicyEngine,
) -> set[str]:
    """Return the tool names a profile advertises under the startup gate.

    Mirrors the assistant wiring: build the full root registrations, apply the
    Google startup filter, then wrap in the profile's policy provider.
    """
    registrations = build_local_tool_registrations(
        definitions=list(TOOLS_DEFINITION),
        implementations=local_tool_implementations,
        metadata_by_name=local_tool_metadata_by_name,
    )
    registrations = filter_oauth_tool_registrations(registrations, state)
    root = LocalToolsProvider(registrations=registrations, embedding_generator=None)
    provider = PolicyEnforcingToolsProvider(wrapped_provider=root, policy_engine=engine)
    definitions_out = await provider.get_tool_definitions()
    return {
        name
        for definition in definitions_out
        if (name := definition.get("function", {}).get("name")) is not None
    }


# --- Profile policy conformance --------------------------------------------


def test_default_assistant_policy_allows_google_tools(tmp_path: Path) -> None:
    config = _load_defaults(tmp_path)
    engine = _profile_engine(config, "default_assistant")
    descriptors = _descriptor_by_name()
    for tool_name in GOOGLE_TOOL_NAMES:
        assert (
            engine.evaluate_for_advertisement(
                descriptors[tool_name], can_confirm=True
            ).decision
            == ToolPolicyDecision.ALLOW
        )


@pytest.mark.parametrize("profile_id", ["email_intake", "reminder"])
def test_ambient_profiles_deny_google_tools(tmp_path: Path, profile_id: str) -> None:
    config = _load_defaults(tmp_path)
    engine = _profile_engine(config, profile_id)
    descriptors = _descriptor_by_name()
    for tool_name in GOOGLE_TOOL_NAMES:
        assert (
            engine.evaluate_for_execution(
                descriptors[tool_name], arguments={}, can_confirm=True
            ).decision
            == ToolPolicyDecision.DENY
        )


# --- Startup registration filter -------------------------------------------


@pytest.mark.asyncio
async def test_enabled_tools_advertised_in_default_assistant(
    tmp_path: Path,
) -> None:
    config = _load_defaults(tmp_path)
    engine = _profile_engine(config, "default_assistant")
    advertised = await _advertised_names(_enabled_state(GOOGLE_TOOL_NAMES), engine)
    assert advertised >= GOOGLE_TOOL_NAMES


@pytest.mark.asyncio
async def test_enabled_tools_not_advertised_in_ambient_profiles(
    tmp_path: Path,
) -> None:
    config = _load_defaults(tmp_path)
    engine = _profile_engine(config, "email_intake")
    advertised = await _advertised_names(_enabled_state(GOOGLE_TOOL_NAMES), engine)
    assert advertised.isdisjoint(GOOGLE_TOOL_NAMES)


@pytest.mark.asyncio
async def test_disabled_integration_drops_tools_everywhere(
    tmp_path: Path,
) -> None:
    config = _load_defaults(tmp_path)
    engine = _profile_engine(config, "default_assistant")
    # Even where policy allows them, the filtered-out tools cannot be advertised.
    advertised = await _advertised_names(_disabled_state(), engine)
    assert advertised.isdisjoint(GOOGLE_TOOL_NAMES)


@pytest.mark.asyncio
async def test_scope_conditional_subset_gmail_only(tmp_path: Path) -> None:
    config = _load_defaults(tmp_path)
    engine = _profile_engine(config, "default_assistant")
    gmail_only = frozenset({
        "gmail_search",
        "gmail_get_message",
        "gmail_get_attachment",
    })
    advertised = await _advertised_names(_enabled_state(gmail_only), engine)
    assert advertised >= gmail_only
    assert "drive_search" not in advertised
    assert "drive_get_file" not in advertised


def test_google_scope_enum_matches_tool_names() -> None:
    # Guard: the five names this test hardcodes match the shipped scope map keys.
    assert frozenset(GOOGLE_TOOL_REQUIRED_SCOPES) == GOOGLE_TOOL_NAMES
    assert GoogleScope.GMAIL_READONLY.value
