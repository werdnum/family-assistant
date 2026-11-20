"""execute_script reaches every shipped profile except those confined against it.

A script calls tools only through the running profile's own provider, so the
grant adds no tool the profile does not already hold. The profiles that withhold
it do so because the script's non-tool APIs (attachment reads by id, Keychute
egress) would cross their confinement.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from family_assistant.assistant import (
    _build_profile_policy_engine,  # noqa: PLC2701 - smallest helper that applies global_tools_policy injection to a profile
)
from family_assistant.config_loader import load_config
from family_assistant.tools.catalog import LOCAL_TOOL_DESCRIPTORS
from family_assistant.tools.policy import ToolPolicyDecision

if TYPE_CHECKING:
    from pathlib import Path

_WITHHELD = frozenset({
    "coder",
    "credential_browser_profile",
    "credential_browser_visual_profile",
    "authenticated_browser_profile",
    "authenticated_browser_visual_profile",
    "media_analyst",
    "memory_curator",
    "telephone_external",
})


def test_execute_script_is_allowed_exactly_outside_confined_profiles(
    tmp_path: Path,
) -> None:
    config = load_config(
        defaults_file_path="defaults.yaml",
        config_file_path=str(tmp_path / "missing-config.yaml"),
    )
    descriptor = next(d for d in LOCAL_TOOL_DESCRIPTORS if d.name == "execute_script")

    decisions = {
        profile.id: _build_profile_policy_engine(
            profile.id,
            profile.tools_policy or config.default_profile_settings.tools_policy,
            None,
            config.global_tools_policy,
            profile.excluded_global_tools,
        )
        .evaluate(descriptor)
        .decision
        for profile in config.service_profiles
        if profile.remote_a2a is None
    }

    assert {
        profile_id
        for profile_id, decision in decisions.items()
        if decision is not ToolPolicyDecision.ALLOW
    } == _WITHHELD
