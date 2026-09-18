"""The shipped `memory_curator` profile's confinement, measured as production computes it.

Slice 3 of docs/design/conversation-memory.md. The curator reads sensitive data
and writes state, so both are confined to memory-labelled notes. Every claim
here is about `defaults.yaml` as loaded, and each is checked through the
function the application itself calls -- `_build_profile_policy_engine` for the
tool set, the profile-filtering the assistant applies for context providers --
rather than by re-reading the YAML, because a confinement that holds only in a
test's own model of the config is not a confinement.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from family_assistant.assistant import (
    _build_profile_policy_engine,  # noqa: PLC2701 - the helper that applies global_tools_policy injection and excluded_global_tools
)
from family_assistant.config_models import CONTEXT_PROVIDER_NAMES
from family_assistant.memory.invariants import MEMORY_LABEL
from family_assistant.storage.base import create_engine_with_sqlite_optimizations
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.notes import NoteReadPolicy
from family_assistant.tools import LOCAL_TOOL_DESCRIPTORS
from family_assistant.tools.policy import ToolPolicyDecision
from family_assistant.tools.types import ToolExecutionContext
from tests.unit.conftest import shipped_profile

if TYPE_CHECKING:
    from family_assistant.config_models import AppConfig, ServiceProfile

pytestmark = pytest.mark.no_db

PROFILE_ID = "memory_curator"


@pytest.fixture(name="curator")
def curator_fixture(shipped_config: AppConfig) -> ServiceProfile:
    return shipped_profile(shipped_config, PROFILE_ID)


def _effective_tool_names(config: AppConfig, profile: ServiceProfile) -> set[str]:
    """The tools the profile advertises, after global injection and exclusions."""
    engine = _build_profile_policy_engine(
        profile.id,
        profile.tools_policy,
        profile.operator_tools_policy,
        config.global_tools_policy,
        profile.excluded_global_tools,
        memory_read=profile.processing_config.memory_read,
    )
    return {
        descriptor.name
        for descriptor in LOCAL_TOOL_DESCRIPTORS
        if engine.evaluate_for_advertisement(descriptor, can_confirm=False).decision
        is ToolPolicyDecision.ALLOW
    }


def test_the_curator_writes_only_memory_notes(curator: ServiceProfile) -> None:
    """The write floor is what makes the repository refuse a household note."""
    processing_config = curator.processing_config

    assert processing_config.required_note_visibility_labels == [MEMORY_LABEL]
    assert processing_config.default_note_visibility_labels == [MEMORY_LABEL]


def test_the_curator_reads_only_memory_notes(curator: ServiceProfile) -> None:
    """Grants plus the read floor, as the derived policy object."""
    processing_config = curator.processing_config

    policy = NoteReadPolicy.for_profile(
        visibility_grants=curator.visibility_grants,
        required_labels=processing_config.required_note_read_labels,
        memory_read=processing_config.memory_read,
    )

    assert policy.required_labels == frozenset({MEMORY_LABEL})
    assert policy.grants == frozenset({MEMORY_LABEL})
    # An unlabelled note and a label-less file skill both pass the grant set;
    # only the floor refuses them.
    assert policy.admits_labels([MEMORY_LABEL]) is True
    assert policy.admits_labels([]) is False
    assert policy.admits_labels(["default"]) is False


def test_the_execution_context_derives_the_same_two_policies(
    curator: ServiceProfile,
) -> None:
    """The profile's config reaches the tools, which is where reads happen.

    The handle is never used: deriving a policy touches no database.
    """
    processing_config = curator.processing_config
    context = ToolExecutionContext(
        conversation_id="c",
        interface_type="internal",
        turn_id="t",
        user_name="curator",
        db_context=Database(
            engine=create_engine_with_sqlite_optimizations(
                "sqlite+aiosqlite:///:memory:"
            )
        ),
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        credential_resolvers=None,
        api_backend=None,
        timezone=ZoneInfo("UTC"),
        visibility_grants=set(curator.visibility_grants),
        required_note_read_labels=processing_config.required_note_read_labels,
        memory_read=processing_config.memory_read,
        required_note_visibility_labels=(
            processing_config.required_note_visibility_labels
        ),
        default_note_visibility_labels=(
            processing_config.default_note_visibility_labels
        ),
        allowed_note_visibility_labels=(
            processing_config.allowed_note_visibility_labels
        ),
    )

    assert context.note_read_policy().required_labels == frozenset({MEMORY_LABEL})
    assert context.note_write_policy().required_labels == [MEMORY_LABEL]


def test_the_curator_holds_exactly_the_memory_tools(
    shipped_config: AppConfig, curator: ServiceProfile
) -> None:
    """Deny-by-default is not enough on its own.

    `global_tools_policy` is injected at the `profile` layer, which outranks the
    `defaults` layer a profile's own `tools_policy` occupies, so the three
    globally granted tools have to be withheld explicitly.
    """
    assert _effective_tool_names(shipped_config, curator) == {
        "get_note",
        "propose_memory_edits",
    }


def test_the_curator_reads_no_document_index_and_deletes_no_note(
    shipped_config: AppConfig, curator: ServiceProfile
) -> None:
    """The two tools whose absence is a design decision, not an omission.

    `search_documents` is the widest path from the indexed corpus into a turn
    with no human in it. `delete_note` is not a write under the confinement
    policy, so it would let the curator remove any note it can see; removals
    are edits in the proposed list.
    """
    effective = _effective_tool_names(shipped_config, curator)

    assert "search_documents" not in effective
    assert "delete_note" not in effective


def test_the_curator_receives_only_the_notes_context_provider(
    curator: ServiceProfile,
) -> None:
    """Every other provider would hand it household data the read floor cannot reach.

    Calendar, contacts, weather and home state do not come from the notes
    table, so no note-read confinement touches them; excluding the providers is
    the only thing that does.
    """
    processing_config = curator.processing_config
    effective_providers = CONTEXT_PROVIDER_NAMES - set(
        processing_config.excluded_context_providers
    )

    assert effective_providers == {"notes"}
    # The exclusions only mean something if the profile receives context at all.
    assert processing_config.include_aggregated_context is True


def test_the_curator_neither_wakes_nor_is_delegated_to(
    curator: ServiceProfile,
) -> None:
    """It is run by the review task, with a rendered transcript as its request.

    A woken turn would carry neither the review's evidence scope nor the store
    revision it read, and an inbound delegation would put an arbitrary request
    in front of a profile whose only job is to grade a transcript.
    """
    processing_config = curator.processing_config

    assert processing_config.allow_wake_llm is False
    assert processing_config.allowed_delegation_sources == []
    assert curator.slash_commands == []


def test_the_curator_runs_a_small_number_of_iterations(
    curator: ServiceProfile,
) -> None:
    """Enough for a few reads, a proposal, one retry and a reply."""
    assert curator.processing_config.max_iterations <= 10
