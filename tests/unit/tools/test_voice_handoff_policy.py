"""The native voice handoff is available in both full-access voice profiles."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from family_assistant.assistant import (
    _build_profile_policy_engine,  # noqa: PLC2701 - verify the effective runtime policy
)
from family_assistant.config_loader import load_config
from family_assistant.tools import LOCAL_TOOL_DESCRIPTORS
from family_assistant.tools.policy import ToolPolicyDecision

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.no_db


@pytest.mark.parametrize("profile_id", ["default_assistant", "complex_tasks"])
def test_voice_handoff_is_advertised_for_foreground_profiles(
    tmp_path: Path, profile_id: str
) -> None:
    config = load_config(
        defaults_file_path="defaults.yaml",
        config_file_path=str(tmp_path / "missing-config.yaml"),
    )
    profile = next(p for p in config.service_profiles if p.id == profile_id)
    engine = _build_profile_policy_engine(
        profile.id,
        profile.tools_policy or config.default_profile_settings.tools_policy,
        profile.operator_tools_policy,
        config.global_tools_policy,
        profile.excluded_global_tools,
        memory_read=config.effective_memory_read(profile),
    )
    descriptor = next(d for d in LOCAL_TOOL_DESCRIPTORS if d.name == "send_to_my_chat")

    assert (
        engine.evaluate_for_advertisement(descriptor, can_confirm=False).decision
        is ToolPolicyDecision.ALLOW
    )
