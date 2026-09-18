"""Which shipped profiles hold `propose_memory_edits`, as production computes it.

Slice 2 of docs/design/conversation-memory.md grants the tool alongside
`add_or_update_note` in the two profiles whose note writes are not
label-confined, and slice 3 adds the curator. A grant is not the whole answer,
though: writing memory requires reading it, so the tool is withheld from a
profile with `memory_read` off wherever a profile's tool policy is assembled.
What is pinned here is therefore the *effective* set -- what
`_build_profile_policy_engine` advertises -- rather than the rules that feed it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from family_assistant.assistant import (
    _build_profile_policy_engine,  # noqa: PLC2701 - the helper that assembles a profile's effective policy, which is what is under test
)
from family_assistant.config_loader import load_config
from family_assistant.tools import LOCAL_TOOL_DESCRIPTORS
from family_assistant.tools.policy import ToolPolicyDecision

if TYPE_CHECKING:
    from pathlib import Path

    from family_assistant.config_models import AppConfig, ServiceProfile
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


def _profile(config: AppConfig, profile_id: str) -> ServiceProfile:
    return next(p for p in config.service_profiles if p.id == profile_id)


def _holds_memory_tool(config: AppConfig, profile_id: str) -> bool:
    """Whether the profile advertises the memory tool, as the assistant builds it."""
    profile = _profile(config, profile_id)
    engine = _build_profile_policy_engine(
        profile.id,
        profile.tools_policy or config.default_profile_settings.tools_policy,
        profile.operator_tools_policy,
        config.global_tools_policy,
        profile.excluded_global_tools,
        memory_read=profile.processing_config.memory_read,
    )
    return (
        engine.evaluate_for_advertisement(
            _descriptor(MEMORY_TOOL), can_confirm=False
        ).decision
        is ToolPolicyDecision.ALLOW
    )


def test_the_memory_tool_is_registered(tmp_path: Path) -> None:
    assert _descriptor(MEMORY_TOOL).name == MEMORY_TOOL


@pytest.mark.parametrize("profile_id", ["default_assistant", "complex_tasks"])
def test_the_shipped_foreground_profiles_hold_the_tool(
    tmp_path: Path, profile_id: str
) -> None:
    """They read memory, so "remember this" reaches memory rather than a note."""
    config = _config(tmp_path)

    assert _profile(config, profile_id).processing_config.memory_read is True
    assert _holds_memory_tool(config, profile_id) is True


@pytest.mark.parametrize("profile_id", ["default_assistant", "complex_tasks"])
def test_the_same_profile_loses_the_tool_when_it_stops_reading_memory(
    tmp_path: Path, profile_id: str
) -> None:
    """The grant stays; `memory_read` is the whole of the difference.

    A deployment that turns memory off for a profile leaves the tool's grant in
    the shipped policy untouched, and the tool must still disappear: it would
    refuse every call it received, and `add_or_update_note` -- which works -- is
    what a "remember this" should reach instead.
    """
    config = _config(tmp_path)
    _profile(config, profile_id).processing_config.memory_read = False

    assert _holds_memory_tool(config, profile_id) is False


def test_the_curator_holds_the_tool(tmp_path: Path) -> None:
    """The shipped profile whose whole job is curating memory."""
    config = _config(tmp_path)

    assert _profile(config, "memory_curator").processing_config.memory_read is True
    assert _holds_memory_tool(config, "memory_curator") is True


@pytest.mark.parametrize("profile_id", ["event_handler", "ops_automation"])
def test_label_confined_profiles_do_not_get_the_memory_tool(
    tmp_path: Path, profile_id: str
) -> None:
    """Their note writes carry a required label a memory note cannot also carry."""
    assert _holds_memory_tool(_config(tmp_path), profile_id) is False
