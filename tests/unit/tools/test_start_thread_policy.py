"""Starting a thread is admitted exactly like a delegation.

Both tools hand a turn to ``target_service_id``, so the shipped policy must give
them the same decision for every target -- including the per-target deny,
review and confirm rules. See docs/design/one-thread-chat-ux.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from family_assistant.assistant import (
    _build_profile_policy_engine,  # noqa: PLC2701 - the helper production uses to apply global policy injection
)
from family_assistant.tools.catalog import LOCAL_TOOL_DESCRIPTORS
from tests.unit.conftest import shipped_profile

if TYPE_CHECKING:
    from family_assistant.config_models import AppConfig
    from family_assistant.tools.metadata import ToolDescriptor

pytestmark = pytest.mark.no_db


def _descriptor(name: str) -> ToolDescriptor:
    for descriptor in LOCAL_TOOL_DESCRIPTORS:
        if descriptor.name == name:
            return descriptor
    raise AssertionError(f"{name} descriptor not found")


@pytest.mark.parametrize(
    "target",
    [
        "reminder",
        "event_handler",
        "telephone_external",
        "engineer",
        "ops_automation",
        "browser_profile",
        "complex_tasks",
    ],
)
def test_start_thread_gets_the_delegation_decision_for_each_target(
    shipped_config: AppConfig, target: str
) -> None:
    profile = shipped_profile(shipped_config, "default_assistant")
    engine = _build_profile_policy_engine(
        profile.id,
        profile.tools_policy,
        profile.operator_tools_policy,
        shipped_config.global_tools_policy,
        profile.excluded_global_tools,
    )

    decisions = {
        name: engine.evaluate_for_execution(
            _descriptor(name),
            arguments={"target_service_id": target},
            can_confirm=True,
        ).decision
        for name in ("delegate_to_service", "start_thread")
    }

    assert decisions["start_thread"] is decisions["delegate_to_service"]
