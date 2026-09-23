"""Shared fixtures for the ambient note admission tests.

See docs/design/ambient-note-admission-at-write-time.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from family_assistant.context_providers import NotesContextProvider
from family_assistant.security.taint import (
    InMemoryTurnTaintTracker,
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.storage.repositories.notes import NoteReadPolicy, NoteWritePolicy
from family_assistant.tools.types import ToolExecutionContext

if TYPE_CHECKING:
    from family_assistant.security.note_provenance import NoteProvenanceStamp
    from family_assistant.services.attachment_registry import AttachmentRegistry
    from family_assistant.storage.database import Database

SKILL_BODY = "---\nname: Pack for a trip\ndescription: How to pack\n---\nSteps."


def state_at(tier: SourceTrustTier, source_id: str = "source") -> TurnTaintState:
    """A turn state carrying one source at ``tier``."""
    return TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.TOOL_OUTPUT,
            source_id=source_id,
            tier=tier,
            labels=frozenset(),
            reason=f"test source at {tier.config_value}",
        )
    )


def tracker_at(tier: SourceTrustTier | None) -> InMemoryTurnTaintTracker:
    """A tracker for a turn at ``tier`` (clean when None)."""
    if tier is None:
        return InMemoryTurnTaintTracker()
    return InMemoryTurnTaintTracker(state_at(tier))


def tool_context(
    db: Database,
    tracker: InMemoryTurnTaintTracker,
    *,
    attachment_registry: AttachmentRegistry | None = None,
) -> ToolExecutionContext:
    """A tool context for a direct tool call in a web turn."""
    return ToolExecutionContext(
        interface_type="web",
        conversation_id="ambient-notes",
        user_name="Test User",
        turn_id="turn-ambient",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=attachment_registry,
        camera_backend=None,
        visibility_grants=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
        taint_tracker=tracker,
    )


async def write_note(
    db: Database,
    title: str,
    content: str,
    *,
    provenance: NoteProvenanceStamp,
    include_in_prompt: bool = True,
) -> None:
    """Write a note directly through the repository."""
    await db.notes.add_or_update(
        title,
        content,
        include_in_prompt,
        # ast-grep-ignore: no-unconstrained-note-write-policy - test seeding, no profile in play
        write_policy=NoteWritePolicy.UNCONSTRAINED,
        provenance=provenance,
    )


def notes_provider(db: Database) -> NotesContextProvider:
    """The notes context provider over ``db``, unconfined."""
    return NotesContextProvider(
        get_db_context_func=lambda: db,
        prompts={},
        # ast-grep-ignore: no-unrestricted-note-read-policy - test helper, no profile in play
        read_policy=NoteReadPolicy.UNRESTRICTED,
    )


async def stored_tier(db: Database, title: str) -> SourceTrustTier:
    """The tier stored on a note row."""
    note = await db.notes.get_by_title(
        title,
        # ast-grep-ignore: no-unrestricted-note-read-policy - test helper, no profile in play
        read_policy=NoteReadPolicy.UNRESTRICTED,
    )
    assert note is not None
    assert note.provenance_metadata is not None
    return TurnTaintState.from_metadata(
        note.provenance_metadata.get("taint_metadata")
    ).max_tier
