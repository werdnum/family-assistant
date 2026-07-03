"""Policy + guard tests for the ops_automation confinement (Phase 2).

Covers the wake_llm guard helper and the shipped ops_automation profile policy
(script-only automations, bounded diagnostics reads, confined note writes).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from family_assistant.actions import (
    ActionType,
    WakeLlmProfileError,
    assert_wake_llm_allowed,
)
from family_assistant.config_loader import load_config
from family_assistant.tools import LOCAL_TOOL_DESCRIPTORS
from family_assistant.tools.policy import PolicyEngine, ToolPolicyDecision

if TYPE_CHECKING:
    from pathlib import Path

    from family_assistant.tools.metadata import ToolDescriptor


# --- wake_llm guard helper ---


def test_guard_allows_wake_llm_when_permitted() -> None:
    # No raise when the profile permits waking the LLM (the default).
    assert_wake_llm_allowed(ActionType.WAKE_LLM, allow_wake_llm=True)


def test_guard_ignores_scripts_even_when_wake_disabled() -> None:
    # Scripts honor the stored profile, so action_type=script is always fine.
    assert_wake_llm_allowed(ActionType.SCRIPT, allow_wake_llm=False)


def test_guard_refuses_wake_llm_from_confined_profile() -> None:
    with pytest.raises(WakeLlmProfileError):
        assert_wake_llm_allowed(ActionType.WAKE_LLM, allow_wake_llm=False)


# --- ops_automation shipped profile policy ---


def _ops_policy_engine(tmp_path: Path) -> PolicyEngine:
    config = load_config(
        defaults_file_path="defaults.yaml",
        config_file_path=str(tmp_path / "missing-config.yaml"),
    )
    profile = next(p for p in config.service_profiles if p.id == "ops_automation")
    assert profile.tools_policy is not None
    return PolicyEngine.from_policy_config(profile.tools_policy)


def _descriptor(name: str) -> ToolDescriptor:
    return next(d for d in LOCAL_TOOL_DESCRIPTORS if d.name == name)


def test_ops_policy_denies_wake_llm_automation(tmp_path: Path) -> None:
    engine = _ops_policy_engine(tmp_path)
    evaluation = engine.evaluate(
        _descriptor("create_automation"),
        arguments={"action_type": "wake_llm"},
    )
    assert evaluation.decision == ToolPolicyDecision.DENY


def test_ops_policy_allows_script_automation(tmp_path: Path) -> None:
    engine = _ops_policy_engine(tmp_path)
    evaluation = engine.evaluate(
        _descriptor("create_automation"),
        arguments={"action_type": "script"},
    )
    assert evaluation.decision == ToolPolicyDecision.ALLOW


def test_ops_policy_allows_diagnostics_and_notes(tmp_path: Path) -> None:
    engine = _ops_policy_engine(tmp_path)
    for name in ("read_error_logs", "add_or_update_note"):
        assert (
            engine.evaluate(_descriptor(name)).decision == ToolPolicyDecision.ALLOW
        ), name


def test_ops_policy_denies_traceback_reads(tmp_path: Path) -> None:
    engine = _ops_policy_engine(tmp_path)
    # Sanitized reads are allowed; explicit tracebacks are denied.
    assert (
        engine.evaluate(_descriptor("read_error_logs")).decision
        == ToolPolicyDecision.ALLOW
    )
    assert (
        engine.evaluate(
            _descriptor("read_error_logs"),
            arguments={"include_tracebacks": True},
        ).decision
        == ToolPolicyDecision.DENY
    )


def test_ops_policy_denies_automation_reads_and_outbound(tmp_path: Path) -> None:
    engine = _ops_policy_engine(tmp_path)
    # get_automation/list_automations read other profiles' automations, so they
    # are not granted; outbound/delegation are denied by default.
    for name in (
        "get_automation",
        "list_automations",
        "send_message_to_user",
        "delegate_to_service",
    ):
        assert engine.evaluate(_descriptor(name)).decision == ToolPolicyDecision.DENY, (
            name
        )


def test_ops_profile_confines_note_writes(tmp_path: Path) -> None:
    config = load_config(
        defaults_file_path="defaults.yaml",
        config_file_path=str(tmp_path / "missing-config.yaml"),
    )
    profile = next(p for p in config.service_profiles if p.id == "ops_automation")
    pc = profile.processing_config
    assert pc.required_note_visibility_labels == ["ops_diagnostics"]
    assert pc.allowed_note_visibility_labels == ["ops_diagnostics"]
    assert pc.allow_wake_llm is False
    assert profile.visibility_grants == ["ops_diagnostics"]
