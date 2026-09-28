"""The shipped `council` and `council_member` profiles, as production loads them.

See docs/design/council.md. The council's promises that configuration has to
keep: every seat on its roster is a real single-model preset its member profile
admits, the procedure is readable by the coordinator and nobody else, neither
profile is handed the household's data, and delegation runs one way only --
coordinator to member, member to the coding sandbox.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from family_assistant.assistant import (
    _build_profile_policy_engine,  # noqa: PLC2701 - the helper that applies global_tools_policy injection and excluded_global_tools
)
from family_assistant.llm.model_tiers import validate_profile_model_tier
from family_assistant.paths import PACKAGE_ROOT
from family_assistant.skills.loader import load_skills_from_directory
from family_assistant.skills.registry import NoteRegistry
from family_assistant.storage.repositories.notes import NoteReadPolicy
from family_assistant.tools import LOCAL_TOOL_DESCRIPTORS
from family_assistant.tools.policy import ToolPolicyDecision
from tests.unit.conftest import shipped_profile

if TYPE_CHECKING:
    from family_assistant.config_models import AppConfig, ServiceProfile

pytestmark = pytest.mark.no_db

SKILL_NAME = "Council Deliberation"
DELEGATE = next(d for d in LOCAL_TOOL_DESCRIPTORS if d.name == "delegate_to_service")


def _roster(config: AppConfig, council: ServiceProfile) -> list[str]:
    """The presets the coordinator's instructions name, in the order named."""
    prompt = council.processing_config.prompts["system_prompt"]
    return [
        name
        for name in re.findall(r'"([a-z0-9_]+)"', prompt)
        if name in config.model_tiers
    ]


def _engine(config: AppConfig, profile: ServiceProfile):  # noqa: ANN202 - the engine type is private to assistant.py
    return _build_profile_policy_engine(
        profile.id,
        profile.tools_policy,
        profile.operator_tools_policy,
        config.global_tools_policy,
        profile.excluded_global_tools,
        memory_read=config.effective_memory_read(profile),
    )


def _can_delegate_to(config: AppConfig, profile: ServiceProfile, target: str) -> bool:
    evaluation = _engine(config, profile).evaluate(
        DELEGATE, arguments={"target_service_id": target, "user_request": "x"}
    )
    return evaluation.decision is ToolPolicyDecision.ALLOW


def _reads_skill(profile: ServiceProfile) -> bool:
    registry = NoteRegistry(
        load_skills_from_directory(PACKAGE_ROOT / "skills" / "builtin")
    )
    policy = NoteReadPolicy.for_profile(
        visibility_grants=profile.visibility_grants,
        required_labels=profile.processing_config.required_note_read_labels,
        memory_read=profile.processing_config.memory_read,
    )
    return registry.get_skill_by_name(SKILL_NAME, policy) is not None


def test_every_seat_is_a_single_model_preset_the_member_admits(
    shipped_config: AppConfig,
) -> None:
    """The roster is prose, so it is checked against what the member will accept.

    A seat the member does not admit fails the delegation at call time, and a
    preset with a fallback could answer as another model under the seat's name.
    """
    council = shipped_profile(shipped_config, "council")
    member = shipped_profile(shipped_config, "council_member")
    roster = _roster(shipped_config, council)

    assert roster == ["gpt_6_sol", "claude_fable_5_1", "kimi_k3", "claude_opus_5_5"]
    admitted = member.delegation_model_tiers or []
    for preset in roster:
        assert preset in admitted
        assert len(shipped_config.model_tiers[preset].chain) == 1


def test_both_profiles_pass_startup_tier_validation(shipped_config: AppConfig) -> None:
    for profile_id in ("council", "council_member"):
        profile = shipped_profile(shipped_config, profile_id)
        assert validate_profile_model_tier(profile, shipped_config.model_tiers)


def test_only_the_coordinator_can_read_the_procedure(shipped_config: AppConfig) -> None:
    """Members are told not to coordinate; the label is what makes that hold."""
    assert _reads_skill(shipped_profile(shipped_config, "council"))
    assert not _reads_skill(shipped_profile(shipped_config, "council_member"))
    assert not _reads_skill(shipped_profile(shipped_config, "default_assistant"))


def test_the_coordinator_cannot_read_household_notes(shipped_config: AppConfig) -> None:
    council = shipped_profile(shipped_config, "council")
    policy = NoteReadPolicy.for_profile(
        visibility_grants=council.visibility_grants,
        required_labels=council.processing_config.required_note_read_labels,
        memory_read=council.processing_config.memory_read,
    )

    assert policy.admits_labels([]) is False
    assert policy.admits_labels(["default"]) is False


@pytest.mark.parametrize("profile_id", ["council", "council_member"])
def test_neither_profile_receives_household_context(
    shipped_config: AppConfig, profile_id: str
) -> None:
    profile = shipped_profile(shipped_config, profile_id)

    assert profile.processing_config.include_aggregated_context is False


def test_delegation_runs_one_way(shipped_config: AppConfig) -> None:
    """Coordinator to member, member to the sandbox, and no way back up.

    Self-delegation is granted to every profile above its own policy, so the
    recursion a policy cannot refuse is refused by who may delegate in.
    """
    council = shipped_profile(shipped_config, "council")
    member = shipped_profile(shipped_config, "council_member")

    assert _can_delegate_to(shipped_config, council, "council_member")
    for target in ("default_assistant", "coder", "complex_tasks"):
        assert not _can_delegate_to(shipped_config, council, target)
    assert "council" not in (council.processing_config.allowed_delegation_sources or [])

    assert _can_delegate_to(shipped_config, member, "coder")
    for target in ("council", "default_assistant"):
        assert not _can_delegate_to(shipped_config, member, target)

    assert member.processing_config.allowed_delegation_sources == ["council"]
    assert member.slash_commands == []


def test_the_council_is_reached_only_by_delegation(shipped_config: AppConfig) -> None:
    """A delegated run is what holds the coordinator's turn open for a whole phase.

    Chosen directly there is no run, so each member's completion would wake the
    coordinator on its own.
    """
    assert shipped_profile(shipped_config, "council").slash_commands == []


@pytest.mark.parametrize(
    ("profile_id", "expected"),
    [
        (
            "council",
            {
                "get_note",
                "get_attachment_info",
                "get_delegation_status",
                "list_delegations",
                "delegate_to_service",
            },
        ),
        (
            "council_member",
            {
                "get_attachment_info",
                "get_delegation_status",
                "list_delegations",
                "delegate_to_service",
            },
        ),
    ],
)
def test_each_profile_holds_exactly_its_local_tools(
    shipped_config: AppConfig, profile_id: str, expected: set[str]
) -> None:
    """Deny-by-default is not enough on its own.

    `global_tools_policy` outranks a profile's own policy, so the globally
    granted tools -- which reach any attachment the user owns, or persist
    model-supplied text -- have to be withheld explicitly.
    """
    engine = _engine(shipped_config, shipped_profile(shipped_config, profile_id))
    advertised = {
        descriptor.name
        for descriptor in LOCAL_TOOL_DESCRIPTORS
        if engine.evaluate_for_advertisement(descriptor, can_confirm=False).decision
        is ToolPolicyDecision.ALLOW
    }

    assert advertised == expected
