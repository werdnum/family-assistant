"""Which shipped profiles may propose memory edits.

Slice 2 of docs/design/conversation-memory.md grants `propose_memory_edits`
alongside `add_or_update_note` in the two profiles whose note writes are not
label-confined: the default assistant (through
``default_profile_settings.tools_policy``) and `complex_tasks`. The confined
profiles -- event handling, ops diagnostics -- are deliberately left out: their
note writes carry a required label that a memory note cannot also carry, so the
grant would advertise a tool that could never succeed. The curator profile
arrives in slice 3.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from family_assistant.config_loader import load_config
from family_assistant.tools import LOCAL_TOOL_DESCRIPTORS
from family_assistant.tools.policy import PolicyEngine, ToolPolicyDecision

if TYPE_CHECKING:
    from pathlib import Path

    from family_assistant.config_models import AppConfig
    from family_assistant.tools.metadata import ToolDescriptor

pytestmark = pytest.mark.no_db

MEMORY_TOOL = "propose_memory_edits"


def _config(tmp_path: Path) -> AppConfig:
    return load_config(
        defaults_file_path="defaults.yaml",
        config_file_path=str(tmp_path / "missing-config.yaml"),
    )


def _descriptor(name: str) -> ToolDescriptor:
    return next(d for d in LOCAL_TOOL_DESCRIPTORS if d.name == name)


def _profile_engine(config: AppConfig, profile_id: str) -> PolicyEngine:
    profile = next(p for p in config.service_profiles if p.id == profile_id)
    policy = profile.tools_policy or config.default_profile_settings.tools_policy
    assert policy is not None
    return PolicyEngine.from_policy_config(policy)


def test_the_memory_tool_is_registered(tmp_path: Path) -> None:
    assert _descriptor(MEMORY_TOOL).name == MEMORY_TOOL


@pytest.mark.parametrize("profile_id", ["default_assistant", "complex_tasks"])
def test_profiles_that_write_ordinary_notes_may_edit_memory(
    tmp_path: Path, profile_id: str
) -> None:
    engine = _profile_engine(_config(tmp_path), profile_id)

    assert engine.evaluate(_descriptor(MEMORY_TOOL)).decision == (
        ToolPolicyDecision.ALLOW
    )


@pytest.mark.parametrize("profile_id", ["event_handler", "ops_automation"])
def test_label_confined_profiles_do_not_get_the_memory_tool(
    tmp_path: Path, profile_id: str
) -> None:
    engine = _profile_engine(_config(tmp_path), profile_id)

    assert engine.evaluate(_descriptor(MEMORY_TOOL)).decision != (
        ToolPolicyDecision.ALLOW
    )
