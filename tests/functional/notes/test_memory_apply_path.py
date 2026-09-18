"""The memory edit-list apply path.

Slice 2 of docs/design/conversation-memory.md: one deterministic path that
validates an edit list against the v1 invariants all-or-nothing, applies it in
one short transaction conditional on the store revision, regenerates the core
note's topic index, and records every change.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from family_assistant.llm.messages import UserMessage
from family_assistant.memory.actor import MemoryActor, MemoryActorKind
from family_assistant.memory.apply import (
    ApplyOutcome,
    apply_memory_edits_atomically,
)
from family_assistant.memory.edits import EvidenceScope, MemoryEdit, MemoryEditOp
from family_assistant.memory.index import INDEX_START_MARKER, strip_topic_index
from family_assistant.memory.invariants import MEMORY_LABEL
from family_assistant.memory.limits import MemoryLimits
from family_assistant.memory.review_context import MemoryReviewContext
from family_assistant.security.taint import (
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.storage.database import Database, DatabaseTransaction
from family_assistant.storage.repositories.notes import NoteReadPolicy, NoteWritePolicy
from family_assistant.tools.memory import propose_memory_edits_tool
from family_assistant.tools.types import ToolExecutionContext

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine

CORE_TITLE = MemoryLimits.DEFAULTS.core_note_title
CONVERSATION = "memory-apply-conv"
OTHER_CONVERSATION = "other-conv"
TURN = "turn-1"
NOW = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)

# Room for the derived index plus a couple of entries, small enough that a
# deliberate overflow reads as one line rather than a wall of text.
LIMITS = MemoryLimits(core_note_max_chars=400, topic_note_max_chars=300)


def _db(engine: AsyncEngine, *, limits: MemoryLimits = LIMITS) -> Database:
    return Database(engine=engine, memory_limits=limits)


async def _seed_turn(
    db: Database,
    *,
    conversation_id: str = CONVERSATION,
    turn_id: str = TURN,
    count: int = 2,
) -> list[int]:
    """Persist ``count`` user messages and return their internal ids."""
    return [
        await db.message_history.add_message(
            UserMessage(content=f"message {index} of {turn_id}"),
            interface_type="web",
            conversation_id=conversation_id,
            timestamp=NOW,
            turn_id=turn_id,
            processing_profile_id="default_assistant",
            user_id="alice",
        )
        for index in range(count)
    ]


def _scope(conversation_id: str = CONVERSATION, turn_id: str = TURN) -> EvidenceScope:
    return EvidenceScope.for_turn(
        interface_type="web", conversation_id=conversation_id, turn_id=turn_id
    )


def _actor() -> MemoryActor:
    return MemoryActor(
        kind=MemoryActorKind.CURATOR,
        identity="memory_curator",
        interface_type="web",
        conversation_id=CONVERSATION,
    )


def _unconfined_read_policy() -> NoteReadPolicy:
    """The read policy of a profile with no grants configured that reads memory."""
    return NoteReadPolicy.for_profile(
        visibility_grants=None, required_labels=None, memory_read=True
    )


def _curator_read_policy() -> NoteReadPolicy:
    """The shipped `memory_curator` read confinement: granted and floored on `memory`."""
    return NoteReadPolicy.for_profile(
        visibility_grants=["memory"], required_labels=["memory"], memory_read=True
    )


def _curator_write_policy() -> NoteWritePolicy:
    """The shipped `memory_curator` write confinement."""
    return NoteWritePolicy(
        visibility_grants={"memory"},
        default_labels=[MEMORY_LABEL],
        required_labels=[MEMORY_LABEL],
        allowed_labels=None,
    )


async def _apply(
    db: Database,
    edits: Sequence[MemoryEdit],
    *,
    expected_revision: int | None = None,
    evidence_scope: EvidenceScope | None = None,
    read_policy: NoteReadPolicy | None = None,
    write_policy: NoteWritePolicy | None = None,
    # ast-grep-ignore: no-dict-any - provenance metadata stores compact runtime taint JSON
    provenance_metadata: dict[str, object] | None = None,
    after_apply: Callable[[DatabaseTransaction], Awaitable[None]] | None = None,
) -> ApplyOutcome:
    revision = (
        await db.memory_store.get_revision()
        if expected_revision is None
        else expected_revision
    )
    return await apply_memory_edits_atomically(
        db,
        edits,
        read_policy=read_policy or _unconfined_read_policy(),
        write_policy=write_policy or NoteWritePolicy.UNCONSTRAINED,
        evidence_scope=evidence_scope or _scope(),
        expected_revision=revision,
        actor=_actor(),
        provenance_metadata=provenance_metadata,
        now=NOW,
        after_apply=after_apply,
    )


async def _entries(db: Database, title: str) -> str:
    note = await db.notes.get_by_title(title, read_policy=NoteReadPolicy.UNRESTRICTED)
    assert note is not None
    return strip_topic_index(note.content)


def _review_context(
    scope: EvidenceScope, expected_revision: int
) -> MemoryReviewContext:
    return MemoryReviewContext(
        evidence_scope=scope,
        expected_revision=expected_revision,
        batch_id=str(uuid.uuid4()),
        interface_type="web",
        conversation_id=CONVERSATION,
        watermark_target=scope.last_internal_id or 0,
    )


def _tool_context(
    db: Database,
    *,
    turn_id: str | None = TURN,
    memory_review: MemoryReviewContext | None = None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="web",
        conversation_id=CONVERSATION,
        user_name="alice",
        turn_id=turn_id,
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        visibility_grants=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
        memory_read=True,
        memory_review=memory_review,
    )


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_add_creates_a_topic_note_that_is_not_always_loaded(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    ids = await _seed_turn(db)

    outcome = await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Sam",
                entry="Sam prefers the tram (Alice, 2026-09-17).",
                message_ids=[ids[0]],
            )
        ],
    )

    assert outcome.applied is True
    note = await db.notes.get_by_title("Sam", read_policy=NoteReadPolicy.UNRESTRICTED)
    assert note is not None
    assert note.include_in_prompt is False
    assert note.visibility_labels == [MEMORY_LABEL]
    assert "Sam prefers the tram" in note.content


@pytest.mark.asyncio
async def test_an_added_entry_carries_its_message_references(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    ids = await _seed_turn(db)

    await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Sam",
                entry="Sam prefers the tram.",
                message_ids=[ids[1], ids[0]],
            )
        ],
    )

    assert f"(refs: #{ids[0]}, #{ids[1]})" in await _entries(db, "Sam")


@pytest.mark.asyncio
async def test_an_entry_that_already_names_its_evidence_is_left_alone(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    ids = await _seed_turn(db, count=1)

    await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Sam",
                entry=f"Sam prefers the tram [see #{ids[0]}].",
                message_ids=[ids[0]],
            )
        ],
    )

    assert "refs:" not in await _entries(db, "Sam")


@pytest.mark.asyncio
async def test_replace_swaps_one_entry_and_recites(db_engine: AsyncEngine) -> None:
    db = _db(db_engine)
    ids = await _seed_turn(db)
    await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Sam",
                entry="Sam takes the bus.",
                message_ids=[ids[0]],
            )
        ],
    )

    await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.REPLACE,
                note_title="Sam",
                target_text=f"Sam takes the bus. (refs: #{ids[0]})",
                entry="Correction: Sam takes the tram.",
                message_ids=[ids[1]],
            )
        ],
    )

    entries = await _entries(db, "Sam")
    assert "takes the bus" not in entries
    assert f"Correction: Sam takes the tram. (refs: #{ids[1]})" in entries


@pytest.mark.asyncio
async def test_a_move_carries_the_entry_and_its_references_verbatim(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    ids = await _seed_turn(db, count=1)
    await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title=CORE_TITLE,
                entry="Sam prefers the tram.",
                message_ids=[ids[0]],
            )
        ],
    )
    entry = f"Sam prefers the tram. (refs: #{ids[0]})"

    outcome = await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.MOVE,
                note_title=CORE_TITLE,
                target_text=entry,
                destination_note_title="Sam",
            )
        ],
    )

    assert outcome.applied is True
    assert entry not in await _entries(db, CORE_TITLE)
    assert entry in await _entries(db, "Sam")


@pytest.mark.asyncio
async def test_a_multi_line_entry_is_one_entry(db_engine: AsyncEngine) -> None:
    db = _db(db_engine)
    ids = await _seed_turn(db, count=1)

    await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Trip",
                entry="Trip to Melbourne is planned.\nThe children need their own room.",
                message_ids=[ids[0]],
            )
        ],
    )

    outcome = await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.REMOVE,
                note_title="Trip",
                target_text=(
                    "Trip to Melbourne is planned.\n"
                    f"The children need their own room. (refs: #{ids[0]})"
                ),
                message_ids=[ids[0]],
            )
        ],
    )

    assert outcome.applied is True, outcome.rejections
    assert not await _entries(db, "Trip")


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evidence_from_another_conversation_is_refused(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    await _seed_turn(db, count=1)
    outside = await _seed_turn(
        db, conversation_id=OTHER_CONVERSATION, turn_id="other-turn", count=1
    )

    outcome = await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Sam",
                entry="Sam likes trams.",
                message_ids=outside,
            )
        ],
    )

    assert outcome.applied is False
    assert f"#{outside[0]}" in outcome.rejections[0].reason
    assert (
        await db.notes.get_by_title("Sam", read_policy=NoteReadPolicy.UNRESTRICTED)
        is None
    )


@pytest.mark.asyncio
async def test_a_cited_message_that_does_not_exist_is_refused(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    await _seed_turn(db, count=1)

    outcome = await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Sam",
                entry="Sam likes trams.",
                message_ids=[999_999],
            )
        ],
    )

    assert outcome.applied is False
    assert "#999999" in outcome.rejections[0].reason


@pytest.mark.asyncio
async def test_one_bad_edit_refuses_the_whole_list(db_engine: AsyncEngine) -> None:
    db = _db(db_engine)
    ids = await _seed_turn(db)
    revision_before = await db.memory_store.get_revision()

    outcome = await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Sam",
                entry="Sam likes trams.",
                message_ids=[ids[0]],
            ),
            MemoryEdit(
                op=MemoryEditOp.REMOVE,
                note_title="Sam",
                target_text="something that is not there",
                message_ids=[ids[1]],
            ),
        ],
    )

    assert outcome.applied is False
    assert (
        await db.notes.get_by_title("Sam", read_policy=NoteReadPolicy.UNRESTRICTED)
        is None
    )
    assert await db.memory_store.get_revision() == revision_before
    assert await db.memory_change_log.get_recent(10) == []


@pytest.mark.asyncio
async def test_a_rejected_list_reports_the_revision_that_survived_it(
    db_engine: AsyncEngine,
) -> None:
    """The number a rejection hands back has to be one a retry can use.

    The apply bumps the store before it stages the edits, so a rejection read
    after that point reports a revision the rollback threw away; a retry
    against it would conflict for ever.
    """
    db = _db(db_engine)
    ids = await _seed_turn(db)
    await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Sam",
                entry="Sam likes trams.",
                message_ids=[ids[0]],
            )
        ],
    )

    rejected = await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Sam",
                entry="Sam also likes the ferry.",
                message_ids=[ids[0]],
            ),
            MemoryEdit(
                op=MemoryEditOp.REMOVE,
                note_title="Sam",
                target_text="something that is not there",
                message_ids=[ids[1]],
            ),
        ],
    )

    assert rejected.applied is False
    assert rejected.conflict is False
    assert rejected.revision == await db.memory_store.get_revision()

    retried = await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Sam",
                entry="Sam also likes the ferry.",
                message_ids=[ids[0]],
            )
        ],
        expected_revision=rejected.revision,
    )

    assert retried.applied is True
    assert retried.conflict is False


@pytest.mark.asyncio
async def test_a_reference_is_not_satisfied_by_a_longer_number(
    db_engine: AsyncEngine,
) -> None:
    """``#12`` is cited; an entry mentioning ``#123`` has not cited it."""
    db = _db(db_engine)
    ids = await _seed_turn(db, count=1)
    cited = ids[0]

    await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Sam",
                entry=(
                    f"Sam prefers the tram, which #{cited}0 and #1{cited} "
                    "are not about."
                ),
                message_ids=[cited],
            )
        ],
    )

    assert f"(refs: #{cited})" in await _entries(db, "Sam")


@pytest.mark.asyncio
async def test_a_result_over_the_note_cap_is_refused(db_engine: AsyncEngine) -> None:
    db = _db(db_engine)
    ids = await _seed_turn(db, count=1)

    outcome = await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Trip",
                entry="y" * 400,
                message_ids=[ids[0]],
            )
        ],
    )

    assert outcome.applied is False
    assert "over its 300-character limit" in outcome.rejections[0].reason
    assert (
        await db.notes.get_by_title("Trip", read_policy=NoteReadPolicy.UNRESTRICTED)
        is None
    )


@pytest.mark.asyncio
async def test_more_edits_than_the_review_allows_are_refused(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine, limits=MemoryLimits(max_edits_per_review=2))
    ids = await _seed_turn(db, count=1)

    outcome = await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Sam",
                entry=f"fact {n}",
                message_ids=[ids[0]],
            )
            for n in range(3)
        ],
    )

    assert outcome.applied is False
    assert "over the limit of 2" in outcome.rejections[0].reason
    assert (
        await db.notes.get_by_title("Sam", read_policy=NoteReadPolicy.UNRESTRICTED)
        is None
    )


@pytest.mark.asyncio
async def test_a_target_note_that_is_not_memory_is_refused(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    ids = await _seed_turn(db, count=1)
    await db.notes.add_or_update(
        "Shopping",
        "- milk",
        False,
        # Admin surface equivalent: this suite is about the memory invariants.
        write_policy=NoteWritePolicy.UNCONSTRAINED,
    )

    outcome = await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Shopping",
                entry="Sam likes trams.",
                message_ids=[ids[0]],
            )
        ],
    )

    assert outcome.applied is False
    assert "not a memory note you can edit" in outcome.rejections[0].reason
    assert await _entries(db, "Shopping") == "- milk"


@pytest.mark.asyncio
async def test_a_missing_target_text_is_refused_quoting_the_current_entries(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    ids = await _seed_turn(db)
    await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Sam",
                entry="Sam prefers the tram.",
                message_ids=[ids[0]],
            )
        ],
    )

    outcome = await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.REMOVE,
                note_title="Sam",
                target_text="Sam likes public transport",
                message_ids=[ids[1]],
            )
        ],
    )

    assert outcome.applied is False
    reason = outcome.rejections[0].reason
    assert "no entry matches" in reason
    assert f"Sam prefers the tram. (refs: #{ids[0]})" in reason


@pytest.mark.asyncio
async def test_an_ambiguous_target_text_is_refused(db_engine: AsyncEngine) -> None:
    db = _db(db_engine)
    ids = await _seed_turn(db, count=1)
    await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Sam",
                entry=f"Sam prefers the tram. #{ids[0]}",
                message_ids=[ids[0]],
            )
        ],
    )
    # A second, identical entry: memory tolerates duplicates in v1, and the
    # apply path must refuse to guess which one a writer meant.
    await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Sam",
                entry=f"Sam prefers the tram. #{ids[0]}",
                message_ids=[ids[0]],
            )
        ],
    )

    outcome = await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.REMOVE,
                note_title="Sam",
                target_text=f"Sam prefers the tram. #{ids[0]}",
                message_ids=[ids[0]],
            )
        ],
    )

    assert outcome.applied is False
    assert "2 entries match" in outcome.rejections[0].reason


@pytest.mark.asyncio
async def test_a_write_from_an_externally_authored_turn_is_refused(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    ids = await _seed_turn(db, count=1)
    tainted = TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.TOOL_OUTPUT,
            source_id="web-fetch",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset(),
            reason="turn read an untrusted web page",
        )
    )

    outcome = await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Trip",
                entry="The hotel was nice.",
                message_ids=[ids[0]],
            )
        ],
        provenance_metadata={"taint_metadata": tainted.to_metadata()},
    )

    assert outcome.applied is False
    assert "outside the household" in outcome.rejections[0].reason
    assert (
        await db.notes.get_by_title("Trip", read_policy=NoteReadPolicy.UNRESTRICTED)
        is None
    )


@pytest.mark.asyncio
async def test_a_stale_expected_revision_is_a_conflict(db_engine: AsyncEngine) -> None:
    db = _db(db_engine)
    ids = await _seed_turn(db)
    read_revision = await db.memory_store.get_revision()
    # A person edits memory while the proposal is being prepared.
    await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Sam",
                entry="Sam prefers the tram.",
                message_ids=[ids[0]],
            )
        ],
    )
    revision_after_person = await db.memory_store.get_revision()

    outcome = await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Routines",
                entry="Bins go out on Tuesday.",
                message_ids=[ids[1]],
            )
        ],
        expected_revision=read_revision,
    )

    assert outcome.applied is False
    assert outcome.conflict is True
    assert (
        await db.notes.get_by_title("Routines", read_policy=NoteReadPolicy.UNRESTRICTED)
        is None
    )
    assert await db.memory_store.get_revision() == revision_after_person


@pytest.mark.asyncio
async def test_the_edit_model_cannot_ask_for_a_second_always_loaded_note(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    ids = await _seed_turn(db, count=1)

    # There is no field for it: an edit names a note and its entries, never the
    # prompt flag or the core-note identity.
    assert "include_in_prompt" not in MemoryEdit.model_fields
    assert not any("prompt" in name for name in MemoryEdit.model_fields)

    await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Routines",
                entry="Bins go out on Tuesday.",
                message_ids=[ids[0]],
            )
        ],
    )

    topic = await db.notes.get_by_title(
        "Routines", read_policy=NoteReadPolicy.UNRESTRICTED
    )
    core = await db.notes.get_by_title(
        CORE_TITLE, read_policy=NoteReadPolicy.UNRESTRICTED
    )
    assert topic is not None
    assert topic.include_in_prompt is False
    assert core is not None
    assert core.include_in_prompt is True


# ---------------------------------------------------------------------------
# The derived topic index
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_hand_edited_index_section_is_overwritten(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    ids = await _seed_turn(db, count=1)
    await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Sam",
                entry="Sam prefers the tram.",
                message_ids=[ids[0]],
            )
        ],
    )

    # The notes UI writes the whole note, index section and all.
    await db.notes.add_or_update(
        CORE_TITLE,
        f"- a standing fact\n\n{INDEX_START_MARKER}\n- Invented Topic\n<!-- /memory:index -->",
        True,
        visibility_labels=[MEMORY_LABEL],
        write_policy=NoteWritePolicy.UNCONSTRAINED,
    )

    core = await db.notes.get_by_title(
        CORE_TITLE, read_policy=NoteReadPolicy.UNRESTRICTED
    )
    assert core is not None
    assert "Invented Topic" not in core.content
    assert "- Sam (changed" in core.content
    assert "- a standing fact" in core.content


@pytest.mark.asyncio
async def test_a_deleted_topic_leaves_the_index(db_engine: AsyncEngine) -> None:
    db = _db(db_engine)
    ids = await _seed_turn(db, count=1)
    await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Sam",
                entry="Sam prefers the tram.",
                message_ids=[ids[0]],
            )
        ],
    )

    listed = await db.notes.get_by_title(
        CORE_TITLE, read_policy=NoteReadPolicy.UNRESTRICTED
    )
    assert listed is not None
    assert "- Sam (changed" in listed.content

    assert await db.notes.delete("Sam") is True

    core = await db.notes.get_by_title(
        CORE_TITLE, read_policy=NoteReadPolicy.UNRESTRICTED
    )
    assert core is not None
    assert "Sam" not in core.content


@pytest.mark.asyncio
async def test_a_renamed_topic_is_renamed_in_the_index(db_engine: AsyncEngine) -> None:
    db = _db(db_engine)
    ids = await _seed_turn(db, count=1)
    await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Sam",
                entry="Sam prefers the tram.",
                message_ids=[ids[0]],
            )
        ],
    )

    await db.notes.rename_and_update(
        "Sam",
        "Sam (family)",
        "- Sam prefers the tram.",
        False,
        write_policy=NoteWritePolicy.UNCONSTRAINED,
    )

    core = await db.notes.get_by_title(
        CORE_TITLE, read_policy=NoteReadPolicy.UNRESTRICTED
    )
    assert core is not None
    assert "- Sam (family) (changed" in core.content


# ---------------------------------------------------------------------------
# The change log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_applied_edit_is_recorded_with_its_evidence(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    ids = await _seed_turn(db)
    await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Sam",
                entry="Sam takes the bus.",
                message_ids=[ids[0]],
            )
        ],
    )

    await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.REPLACE,
                note_title="Sam",
                target_text=f"Sam takes the bus. (refs: #{ids[0]})",
                entry="Correction: Sam takes the tram.",
                message_ids=[ids[1]],
            )
        ],
    )

    rows = await db.memory_change_log.get_recent(10)
    assert [row.op for row in rows] == ["replace", "add"]
    replaced = rows[0]
    assert replaced.before_text == f"Sam takes the bus. (refs: #{ids[0]})"
    assert replaced.after_text == f"Correction: Sam takes the tram. (refs: #{ids[1]})"
    assert replaced.evidence_message_ids == [ids[1]]
    assert replaced.actor_kind == "curator"
    assert replaced.outcome == "applied"
    assert replaced.note_title == "Sam"


@pytest.mark.asyncio
async def test_a_review_outcome_with_no_edit_can_be_recorded(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)

    await db.memory_change_log.add_review_outcome(
        batch_id="batch-1",
        actor=_actor(),
        outcome="skipped",
        reason="The stretch carried unknown-external content.",
        now=NOW,
    )

    rows = await db.memory_change_log.get_recent(10)
    assert len(rows) == 1
    assert rows[0].outcome == "skipped"
    assert rows[0].op is None
    assert "unknown-external" in (rows[0].reason or "")


# ---------------------------------------------------------------------------
# The composition seam slice 5 needs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_after_apply_runs_in_the_same_transaction(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    ids = await _seed_turn(db, count=1)
    seen: list[str] = []

    async def _hook(txn: DatabaseTransaction) -> None:
        note = await txn.notes.get_by_title(
            "Sam", read_policy=NoteReadPolicy.UNRESTRICTED
        )
        assert note is not None
        seen.append(note.content)

    outcome = await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Sam",
                entry="Sam prefers the tram.",
                message_ids=[ids[0]],
            )
        ],
        after_apply=_hook,
    )

    assert outcome.applied is True
    assert seen and "Sam prefers the tram" in seen[0]


@pytest.mark.asyncio
async def test_a_failing_after_apply_hook_rolls_the_edits_back(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    ids = await _seed_turn(db, count=1)
    revision_before = await db.memory_store.get_revision()

    async def _hook(_txn: DatabaseTransaction) -> None:
        raise RuntimeError("the watermark could not be advanced")

    with pytest.raises(RuntimeError, match="watermark"):
        await _apply(
            db,
            [
                MemoryEdit(
                    op=MemoryEditOp.ADD,
                    note_title="Sam",
                    entry="Sam prefers the tram.",
                    message_ids=[ids[0]],
                )
            ],
            after_apply=_hook,
        )

    assert (
        await db.notes.get_by_title("Sam", read_policy=NoteReadPolicy.UNRESTRICTED)
        is None
    )
    assert await db.memory_store.get_revision() == revision_before
    assert await db.memory_change_log.get_recent(10) == []


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_tool_applies_a_list_citing_the_current_turn(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    ids = await _seed_turn(db, count=1)

    result = await propose_memory_edits_tool(
        _tool_context(db),
        edits=[
            {
                "op": "add",
                "note_title": "Sam",
                "entry": "Sam prefers the tram.",
                "message_ids": [ids[0]],
            }
        ],
    )

    assert "Applied" in result.get_text()
    assert "Sam prefers the tram" in await _entries(db, "Sam")


@pytest.mark.asyncio
async def test_the_tool_result_text_carries_the_rejection_reasons(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    ids = await _seed_turn(db, count=1)
    outside = await _seed_turn(
        db, conversation_id=OTHER_CONVERSATION, turn_id="other-turn", count=1
    )

    result = await propose_memory_edits_tool(
        _tool_context(db),
        edits=[
            {
                "op": "add",
                "note_title": "Sam",
                "entry": "Sam prefers the tram.",
                "message_ids": [ids[0]],
            },
            {
                "op": "add",
                "note_title": "Routines",
                "entry": "Bins go out on Tuesday.",
                "message_ids": [outside[0]],
            },
        ],
    )

    text = result.get_text()
    assert "No memory edits were applied" in text
    assert f"#{outside[0]}" in text
    assert "edit 2" in text
    assert (
        await db.notes.get_by_title("Sam", read_policy=NoteReadPolicy.UNRESTRICTED)
        is None
    )


@pytest.mark.asyncio
async def test_the_tool_quotes_the_current_entries_on_a_target_text_miss(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    ids = await _seed_turn(db, count=1)
    await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Sam",
                entry="Sam prefers the tram.",
                message_ids=[ids[0]],
            )
        ],
    )

    result = await propose_memory_edits_tool(
        _tool_context(db),
        edits=[
            {
                "op": "remove",
                "note_title": "Sam",
                "target_text": "Sam likes public transport",
                "message_ids": [ids[0]],
            }
        ],
    )

    assert f"Sam prefers the tram. (refs: #{ids[0]})" in result.get_text()


@pytest.mark.asyncio
async def test_the_tool_uses_the_supplied_scope_and_revision(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    reviewed = await _seed_turn(db, turn_id="reviewed-turn", count=2)
    read_revision = await db.memory_store.get_revision()

    result = await propose_memory_edits_tool(
        _tool_context(
            db,
            turn_id="curator-run",
            memory_review=_review_context(
                EvidenceScope.for_stretch(
                    interface_type="web",
                    conversation_id=CONVERSATION,
                    first_internal_id=reviewed[0],
                    last_internal_id=reviewed[-1],
                ),
                read_revision,
            ),
        ),
        edits=[
            {
                "op": "add",
                "note_title": "Sam",
                "entry": "Sam prefers the tram.",
                "message_ids": [reviewed[1]],
            }
        ],
    )

    assert "Applied" in result.get_text()
    rows = await db.memory_change_log.get_recent(10)
    assert rows[0].actor_kind == "curator"


@pytest.mark.asyncio
async def test_the_tool_refuses_a_malformed_proposal(db_engine: AsyncEngine) -> None:
    db = _db(db_engine)
    await _seed_turn(db, count=1)

    result = await propose_memory_edits_tool(
        _tool_context(db),
        edits=[{"op": "add", "note_title": "Sam", "entry": "no evidence"}],
    )

    assert "malformed" in result.get_text()
    assert (
        await db.notes.get_by_title("Sam", read_policy=NoteReadPolicy.UNRESTRICTED)
        is None
    )


# ---------------------------------------------------------------------------
# The caller's own confinement
# ---------------------------------------------------------------------------

HIDDEN_ENTRY = "Alice's counselling is on Tuesdays (Alice, 2026-09-10)."


async def _seed_hidden_memory_note(db: Database) -> None:
    """A memory note labelled beyond the curator's grants, so it hides from it."""
    await db.notes.add_or_update(
        "Private",
        f"- {HIDDEN_ENTRY}",
        False,
        visibility_labels=[MEMORY_LABEL, "private"],
        # Admin surface equivalent: the note is seeded, not written by a profile.
        write_policy=NoteWritePolicy.UNCONSTRAINED,
    )


@pytest.mark.asyncio
async def test_a_memory_note_the_caller_cannot_see_is_not_quoted_back(
    db_engine: AsyncEngine,
) -> None:
    """A target_text miss must not become a read of a note get_note hides."""
    db = _db(db_engine)
    ids = await _seed_turn(db, count=1)
    await _seed_hidden_memory_note(db)

    outcome = await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.REMOVE,
                note_title="Private",
                target_text="something the writer never read",
                message_ids=[ids[0]],
            )
        ],
        read_policy=_curator_read_policy(),
        write_policy=_curator_write_policy(),
    )

    assert outcome.applied is False
    reason = outcome.rejections[0].reason
    assert "not a memory note you can edit" in reason
    assert HIDDEN_ENTRY not in reason
    assert "counselling" not in reason


@pytest.mark.asyncio
async def test_an_entry_of_a_note_the_caller_cannot_see_is_not_removed(
    db_engine: AsyncEngine,
) -> None:
    """The other half: quoting the entry exactly must not remove it either."""
    db = _db(db_engine)
    ids = await _seed_turn(db, count=1)
    await _seed_hidden_memory_note(db)

    outcome = await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.REMOVE,
                note_title="Private",
                target_text=HIDDEN_ENTRY,
                message_ids=[ids[0]],
            )
        ],
        read_policy=_curator_read_policy(),
        write_policy=_curator_write_policy(),
    )

    assert outcome.applied is False
    assert "not a memory note you can edit" in outcome.rejections[0].reason
    assert await _entries(db, "Private") == f"- {HIDDEN_ENTRY}"


@pytest.mark.asyncio
async def test_a_move_destination_the_caller_cannot_see_is_refused(
    db_engine: AsyncEngine,
) -> None:
    """A destination is resolved under the same read policy as a target."""
    db = _db(db_engine)
    ids = await _seed_turn(db, count=1)
    await _seed_hidden_memory_note(db)
    await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Sam",
                entry="Sam prefers the tram.",
                message_ids=[ids[0]],
            )
        ],
        read_policy=_curator_read_policy(),
        write_policy=_curator_write_policy(),
    )

    outcome = await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.MOVE,
                note_title="Sam",
                target_text=f"Sam prefers the tram. (refs: #{ids[0]})",
                destination_note_title="Private",
            )
        ],
        read_policy=_curator_read_policy(),
        write_policy=_curator_write_policy(),
    )

    assert outcome.applied is False
    assert "not a memory note you can edit" in outcome.rejections[0].reason
    assert await _entries(db, "Private") == f"- {HIDDEN_ENTRY}"
    assert "Sam prefers the tram." in await _entries(db, "Sam")


@pytest.mark.asyncio
async def test_the_confined_caller_still_edits_the_memory_notes_it_reads(
    db_engine: AsyncEngine,
) -> None:
    """The curator's own grants still reach the core note and its topic notes."""
    db = _db(db_engine)
    ids = await _seed_turn(db, count=1)
    await _seed_hidden_memory_note(db)

    outcome = await _apply(
        db,
        [
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title=CORE_TITLE,
                entry="The household eats at 6pm (Alice, 2026-09-17).",
                message_ids=[ids[0]],
            ),
            MemoryEdit(
                op=MemoryEditOp.ADD,
                note_title="Sam",
                entry="Sam prefers the tram (Alice, 2026-09-17).",
                message_ids=[ids[0]],
            ),
        ],
        read_policy=_curator_read_policy(),
        write_policy=_curator_write_policy(),
    )

    assert outcome.applied is True
    assert "The household eats at 6pm" in await _entries(db, CORE_TITLE)
    assert "Sam prefers the tram" in await _entries(db, "Sam")
    sam = await db.notes.get_by_title("Sam", read_policy=NoteReadPolicy.UNRESTRICTED)
    assert sam is not None
    assert sam.visibility_labels == [MEMORY_LABEL]


# ---------------------------------------------------------------------------
# A batch is judged on its final state
# ---------------------------------------------------------------------------

# Sized so that the core note holds both entries *without* the derived index,
# but not with it: the batch below is over the cap in the middle and inside it
# at the end.
CROWDED = MemoryLimits(core_note_max_chars=250, topic_note_max_chars=400)
BULKY_ENTRY = "L" * 150
KEPT_ENTRY = "The household eats at 6pm."


async def _seed_crowded_core(db: Database) -> None:
    """A core note that only fits its topic index once the bulky entry leaves."""
    await db.notes.add_or_update(
        CORE_TITLE,
        f"- {KEPT_ENTRY}\n- {BULKY_ENTRY}",
        True,
        visibility_labels=[MEMORY_LABEL],
        # Admin surface equivalent: the note is seeded, not written by a profile.
        write_policy=NoteWritePolicy.UNCONSTRAINED,
    )


def _make_room_edits(message_id: int) -> dict[str, MemoryEdit]:
    """The two edits that add a topic and move the core's bulky entry into it."""
    return {
        "add": MemoryEdit(
            op=MemoryEditOp.ADD,
            note_title="Trips",
            entry="A trip to Perth in October (Alice, 2026-09-17).",
            message_ids=[message_id],
        ),
        "move": MemoryEdit(
            op=MemoryEditOp.MOVE,
            note_title=CORE_TITLE,
            target_text=BULKY_ENTRY,
            destination_note_title="Trips",
        ),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [("move", "add"), ("add", "move")])
async def test_a_batch_is_judged_on_its_final_state_whatever_its_order(
    db_engine: AsyncEngine, order: tuple[str, str]
) -> None:
    """The core note is over its cap mid-batch in one order and not in the other.

    Regenerating the index per note write made the same list applicable or
    refused depending on which edit came first.
    """
    db = _db(db_engine, limits=CROWDED)
    ids = await _seed_turn(db, count=1)
    await _seed_crowded_core(db)
    edits = _make_room_edits(ids[0])

    outcome = await _apply(db, [edits[name] for name in order])

    assert outcome.applied is True, outcome.rejections
    assert await _entries(db, CORE_TITLE) == f"- {KEPT_ENTRY}"
    trips = await _entries(db, "Trips")
    assert BULKY_ENTRY in trips
    assert "A trip to Perth in October" in trips
    core = await db.notes.get_by_title(
        CORE_TITLE, read_policy=NoteReadPolicy.UNRESTRICTED
    )
    assert core is not None
    assert "- Trips (changed" in core.content
    assert len(core.content) <= CROWDED.core_note_max_chars
