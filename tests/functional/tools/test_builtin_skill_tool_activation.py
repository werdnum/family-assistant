"""Built-in skills must declare tools the default profile can actually activate.

A skill's ``activate_tools`` frontmatter is auto-applied when the assistant loads
the skill. Activation is a no-op unless the tool is (a) a real local tool, (b)
listed in ``tools_config.on_demand_local_tools`` -- ``activate_tools`` only ever
reveals on-demand tools -- and (c) advertisable under the loading profile's
policy, which activation re-checks before marking a tool active.

A skill that names a tool failing any of those silently teaches the model to
reach for something it will never receive, which is exactly how the
``automation_creation`` profile ended up authoring automations that woke without
the tools they needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from family_assistant.skills.loader import load_skills_from_directory
from family_assistant.tools import LOCAL_TOOL_DESCRIPTORS, PolicyEngine
from family_assistant.tools.policy import ToolPolicyDecision
from tests.functional.tools.test_defaults_tool_policy_parity import (
    _load_resolved_profiles,
)

if TYPE_CHECKING:
    from family_assistant.skills.types import ParsedSkill

_BUILTIN_SKILLS_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "family_assistant"
    / "skills"
    / "builtin"
)

_AUTOMATION_SKILL_NAME = "Automation Creation"


def _builtin_skills() -> list[ParsedSkill]:
    skills = load_skills_from_directory(_BUILTIN_SKILLS_DIR)
    assert skills, "no built-in skills loaded"
    return skills


def _skills_with_activations() -> list[ParsedSkill]:
    return [skill for skill in _builtin_skills() if skill.activate_tools]


@pytest.mark.parametrize(
    "skill",
    _skills_with_activations(),
    ids=lambda skill: skill.name,
)
def test_builtin_skill_activatable_by_default_profile(skill: ParsedSkill) -> None:
    default_settings, _ = _load_resolved_profiles()
    descriptors_by_name = {
        descriptor.name: descriptor for descriptor in LOCAL_TOOL_DESCRIPTORS
    }
    on_demand = set(default_settings.tools_config.on_demand_local_tools)
    engine = PolicyEngine.from_policy_config(default_settings.tools_policy)

    for tool_name in skill.activate_tools:
        descriptor = descriptors_by_name.get(tool_name)
        assert descriptor is not None, (
            f"skill {skill.name!r} activates unknown tool {tool_name!r}"
        )
        assert tool_name in on_demand, (
            f"skill {skill.name!r} activates {tool_name!r}, which is not in "
            "on_demand_local_tools, so activation cannot reveal it"
        )
        decision = engine.evaluate_for_advertisement(
            descriptor,
            can_confirm=False,
        ).decision
        assert decision == ToolPolicyDecision.ALLOW, (
            f"skill {skill.name!r} activates {tool_name!r}, which the default "
            f"profile policy resolves to {decision}"
        )


def test_automation_skill_covers_the_automation_tools() -> None:
    """The skill replaces the removed ``automation_creation`` profile's tool set."""
    skills_by_name = {skill.name: skill for skill in _builtin_skills()}
    skill = skills_by_name.get(_AUTOMATION_SKILL_NAME)
    assert skill is not None, f"missing built-in skill {_AUTOMATION_SKILL_NAME!r}"

    expected = {
        "create_automation",
        "update_automation",
        "enable_automation",
        "disable_automation",
        "delete_automation",
        "list_automations",
        "get_automation",
        "get_automation_stats",
        "test_event_listener",
        "query_recent_events",
    }
    assert expected <= set(skill.activate_tools)


def test_automation_creation_profile_is_gone() -> None:
    """Automation authoring happens in the main assistant, not a delegate."""
    _, profiles = _load_resolved_profiles()
    assert "automation_creation" not in {profile.id for profile in profiles}


def test_default_profile_can_create_automations_without_confirmation() -> None:
    """A profile that authors automations needs the write tools, not just reads.

    The default profile previously advertised only the read-only automation tools
    while its prompt told it to call ``create_automation``, which is why
    automation creation had to be delegated at all.
    """
    default_settings, _ = _load_resolved_profiles()
    engine = PolicyEngine.from_policy_config(default_settings.tools_policy)
    descriptors_by_name = {
        descriptor.name: descriptor for descriptor in LOCAL_TOOL_DESCRIPTORS
    }

    for tool_name in ("create_automation", "update_automation", "delete_automation"):
        descriptor = descriptors_by_name[tool_name]
        assert (
            engine.evaluate_for_advertisement(
                descriptor,
                can_confirm=False,
            ).decision
            == ToolPolicyDecision.ALLOW
        ), tool_name
