from __future__ import annotations

from typing import TYPE_CHECKING

from family_assistant.config_loader import load_config
from family_assistant.tools import LOCAL_TOOL_DESCRIPTORS
from family_assistant.tools.policy import (
    PolicyEngine,
    ToolPolicyDecision,
)

if TYPE_CHECKING:
    from pathlib import Path

    from family_assistant.tools.metadata import ToolDescriptor


def _email_intake_policy_engine(tmp_path: Path) -> PolicyEngine:
    config = load_config(
        defaults_file_path="defaults.yaml",
        config_file_path=str(tmp_path / "missing-config.yaml"),
    )
    profile = next(
        service_profile
        for service_profile in config.service_profiles
        if service_profile.id == "email_intake"
    )
    assert profile.tools_policy is not None
    return PolicyEngine.from_policy_config(profile.tools_policy)


def _descriptor_by_name() -> dict[str, ToolDescriptor]:
    return {descriptor.name: descriptor for descriptor in LOCAL_TOOL_DESCRIPTORS}


def test_email_intake_policy_advertises_read_tools_and_confirmable_writes(
    tmp_path: Path,
) -> None:
    """The email profile should expose bounded reads and only confirmable writes."""
    engine = _email_intake_policy_engine(tmp_path)
    descriptors = _descriptor_by_name()

    allowed_tools = {
        "search_calendar_events",
        "get_note",
        "list_notes",
        "search_documents",
        "get_full_document_content",
        "get_message_history",
        "list_pending_callbacks",
    }
    confirmable_tools = {
        "add_calendar_event",
        "modify_calendar_event",
        "add_or_update_note",
        "schedule_reminder",
        "schedule_future_callback",
        "modify_pending_callback",
        "send_message_to_user",
        "ingest_document_from_url",
    }

    for tool_name in allowed_tools:
        assert (
            engine.evaluate_for_advertisement(
                descriptors[tool_name],
                can_confirm=False,
            ).decision
            == ToolPolicyDecision.ALLOW
        )

    for tool_name in confirmable_tools:
        assert (
            engine.evaluate_for_advertisement(
                descriptors[tool_name],
                can_confirm=True,
            ).decision
            == ToolPolicyDecision.CONFIRM
        )
        assert (
            engine.evaluate_for_advertisement(
                descriptors[tool_name],
                can_confirm=False,
            ).decision
            == ToolPolicyDecision.DENY
        )


def test_email_intake_policy_blocks_high_risk_tool_classes(tmp_path: Path) -> None:
    """Dangerous tags should stay denied in the email-originated profile."""
    engine = _email_intake_policy_engine(tmp_path)
    descriptors = _descriptor_by_name()

    denied_tools = {
        "delete_calendar_event",
        "delete_note",
        "execute_script",
        "save_script",
        "test_script_with_simulated_tools",
        "spawn_worker",
        "delegate_to_service",
        "create_automation",
        "schedule_action",
        "browser_open",
        "attach_to_response",
        "generate_image",
        "generate_video",
        "download_media",
        "mqtt_publish",
    }

    for tool_name in denied_tools:
        assert (
            engine.evaluate_for_execution(
                descriptors[tool_name],
                arguments={},
                can_confirm=True,
            ).decision
            == ToolPolicyDecision.DENY
        )
