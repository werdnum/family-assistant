"""The memory review, from a settled conversation to entries in the notes.

Slice 5 of docs/design/conversation-memory.md, and its work-plan item 1. The
curator profile, its tools and the apply path are all real here; only the model
is a fake, and what it proposes is computed from the message ids the review put
in front of it, so a test that renders the wrong stretch fails on the citation
rather than on an assertion about text.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa

from family_assistant.memory.due import NO_WATERMARK, select_review_rows
from family_assistant.memory.index import strip_topic_index
from family_assistant.memory.invariants import MEMORY_LABEL
from family_assistant.memory.review import (
    MemoryReviewResult,
    make_memory_review_handler,
    run_memory_review,
)
from family_assistant.memory.sweep import (
    MEMORY_REVIEW_TASK_TYPE,
    memory_review_task_id,
    run_memory_review_sweep,
)
from family_assistant.memory.transcript import UNFINISHED_TURN_MARKER
from family_assistant.security.note_provenance import NoteProvenanceStamp
from family_assistant.security.taint import (
    SourceTrustTier,
    TurnTaintState,
    is_externally_authored,
)
from family_assistant.storage.database import Database
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.tasks import TaskPriority
from family_assistant.tools.types import ToolExecutionContext
from family_assistant.utils.clock import MockClock
from tests.functional.memory.curator_harness import (
    CONTRIBUTOR,
    CONVERSATION,
    CURATOR_READ_POLICY,
    ENABLED_AT,
    NOW,
    PERSON_WRITE_POLICY,
    SETTINGS,
    WEB,
    CuratorScript,
    curator_llm,
    curator_service,
    enable_contribution,
    memory_db,
    review_limits,
    seed_turn,
    tool_results,
)
from tests.helpers import wait_for_condition, wait_for_tasks_to_complete

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.memory.limits import MemoryLimits
    from family_assistant.processing import ProcessingService
    from family_assistant.storage.repositories.memory_change_log import (
        MemoryChangeLogEntry,
    )
    from family_assistant.storage.types import MessageHistoryRow
    from family_assistant.task_worker import TaskWorker

CORE_TITLE = review_limits().core_note_title


def _context(
    db: Database, service: ProcessingService, *, now: datetime = NOW
) -> ToolExecutionContext:
    """What the task worker hands a handler, for a review run without one."""
    return ToolExecutionContext(
        interface_type="internal",
        conversation_id="memory-review",
        user_name="system",
        turn_id=None,
        db_context=db,
        processing_service=service,
        clock=MockClock(now),
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        visibility_grants=None,
        timezone=service.service_config.timezone,
        credential_resolvers=None,
        api_backend=None,
    )


async def _review(
    db: Database,
    service: ProcessingService,
    *,
    limits: MemoryLimits,
    conversation_id: str = CONVERSATION,
    now: datetime = NOW,
) -> MemoryReviewResult:
    return await run_memory_review(
        _context(db, service, now=now),
        interface_type=WEB,
        conversation_id=conversation_id,
        settings=SETTINGS,
        configured_contributors={CONTRIBUTOR},
        limits=limits,
    )


async def _note(db: Database, title: str) -> str:
    note = await db.notes.get_by_title(title, read_policy=CURATOR_READ_POLICY)
    assert note is not None, f"expected a memory note titled {title!r}"
    return note.content


async def _watermark(db: Database, conversation_id: str = CONVERSATION) -> int:
    row = await db.memory_review.get_watermark(
        interface_type=WEB, conversation_id=conversation_id
    )
    return row.last_reviewed_internal_id if row is not None else NO_WATERMARK


async def _outcomes(db: Database) -> list[MemoryChangeLogEntry]:
    return await db.memory_change_log.get_recent(50)


# ---------------------------------------------------------------------------
# The headline: sweep, review, notes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_idle_conversation_becomes_memory_entries(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """The whole mechanism, driven by the real queue.

    The sweep finds the conversation due, the worker runs the review, and the
    curator's entry lands in a memory-labelled topic note carrying the date and
    speaker it wrote and the message reference the apply path appended -- with
    the core note's derived index updated to point at the new topic.
    """
    limits = review_limits()
    db = memory_db(db_engine, limits)
    script = CuratorScript()
    service = curator_service(db_engine, curator_llm(script))

    worker, new_task_event, _ = task_worker_manager(
        processing_service=service, chat_interface=MagicMock()
    )
    worker.register_task_handler(
        MEMORY_REVIEW_TASK_TYPE,
        make_memory_review_handler(
            settings=SETTINGS,
            configured_contributors={CONTRIBUTOR},
            limits=limits,
        ),
    )

    await enable_contribution(db)
    await seed_turn(db, turn_id="turn-1", said="we always take the tram")
    enqueued = await run_memory_review_sweep(
        _context(db, service),
        settings=SETTINGS,
        configured_contributors={CONTRIBUTOR},
    )
    assert enqueued == 1
    new_task_event.set()
    await wait_for_tasks_to_complete(db_engine, task_types={MEMORY_REVIEW_TASK_TYPE})

    entries = strip_topic_index(await _note(db, script.note_title))
    assert "Alice said on 2026-09-17" in entries
    assert "takes the tram" in entries
    assert "(refs: #" in entries
    assert script.note_title in await _note(db, CORE_TITLE)


@pytest.mark.asyncio
async def test_the_written_note_is_a_memory_topic_note(
    db_engine: AsyncEngine,
) -> None:
    limits = review_limits()
    db = memory_db(db_engine, limits)
    service = curator_service(db_engine, curator_llm(CuratorScript()))
    await enable_contribution(db)
    await seed_turn(db, turn_id="turn-1", said="we always take the tram")

    await _review(db, service, limits=limits)

    note = await db.notes.get_by_title("Sam", read_policy=CURATOR_READ_POLICY)
    assert note is not None
    assert note.visibility_labels == [MEMORY_LABEL]
    assert note.include_in_prompt is False


# ---------------------------------------------------------------------------
# What the curator is shown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rows_before_the_enablement_moment_are_never_shown(
    db_engine: AsyncEngine,
) -> None:
    """Turning contribution on learns from what is said next, not from history."""
    limits = review_limits()
    db = memory_db(db_engine, limits)
    script = CuratorScript()
    service = curator_service(db_engine, curator_llm(script))

    await enable_contribution(db)
    await seed_turn(
        db,
        turn_id="old-turn",
        said="a thing said before contribution was turned on",
        at=ENABLED_AT - timedelta(days=1),
    )
    await seed_turn(db, turn_id="turn-1", said="we always take the tram")

    await _review(db, service, limits=limits)

    assert "before contribution was turned on" not in script.requests[0]
    assert "we always take the tram" in script.requests[0]


@pytest.mark.asyncio
async def test_a_memory_note_beyond_the_curators_grants_is_not_in_its_request(
    db_engine: AsyncEngine,
) -> None:
    """Carrying `memory` is not enough: the reader's grants decide as well.

    The request embeds topic notes whole, so a note the curator's own get_note
    hides has to be missing from it too -- otherwise the confinement holds for
    the tool and not for the far larger channel beside it.
    """
    limits = review_limits()
    db = memory_db(db_engine, limits)
    script = CuratorScript()
    service = curator_service(db_engine, curator_llm(script))

    await enable_contribution(db)
    await db.notes.add_or_update(
        title="Transport",
        content="- The family always takes the tram.",
        include_in_prompt=False,
        visibility_labels=[MEMORY_LABEL],
        write_policy=PERSON_WRITE_POLICY,
        provenance=NoteProvenanceStamp.internal(),
    )
    await db.notes.add_or_update(
        title="Private",
        content="- Alice's counselling is on Tuesdays.",
        include_in_prompt=False,
        visibility_labels=[MEMORY_LABEL, "private"],
        write_policy=PERSON_WRITE_POLICY,
        provenance=NoteProvenanceStamp.internal(),
    )
    await seed_turn(db, turn_id="turn-1", said="we always take the tram")

    await _review(db, service, limits=limits)

    assert "always takes the tram" in script.requests[0]
    assert "counselling" not in script.requests[0]


@pytest.mark.asyncio
async def test_a_second_review_shows_only_what_is_new(db_engine: AsyncEngine) -> None:
    limits = review_limits()
    db = memory_db(db_engine, limits)
    script = CuratorScript()
    service = curator_service(db_engine, curator_llm(script))

    await enable_contribution(db)
    await seed_turn(db, turn_id="turn-1", said="we always take the tram")
    await _review(db, service, limits=limits)
    await seed_turn(
        db,
        turn_id="turn-2",
        said="Sam starts swimming on Tuesdays",
        at=NOW - timedelta(minutes=40),
    )

    await _review(db, service, limits=limits)

    assert "we always take the tram" not in script.requests[1]
    assert "swimming on Tuesdays" in script.requests[1]


@pytest.mark.asyncio
async def test_an_unfinished_turn_is_not_reviewed_until_a_later_one_completes(
    db_engine: AsyncEngine,
) -> None:
    """The whole turn-lifecycle model in v1, in one conversation's life.

    Arranged as one act with two observations of the same run: the first
    review's request, and the watermark it left. A turn whose reply has not
    landed ends the chunk before itself, so neither is reviewed.
    """
    limits = review_limits()
    db = memory_db(db_engine, limits)
    script = CuratorScript()
    service = curator_service(db_engine, curator_llm(script))

    await enable_contribution(db)
    settled = await seed_turn(db, turn_id="turn-1", said="we always take the tram")
    await seed_turn(
        db,
        turn_id="turn-2",
        said="about the holiday --",
        replied=None,
        at=NOW - timedelta(minutes=40),
    )

    await _review(db, service, limits=limits)

    assert "about the holiday" not in script.requests[0]
    assert await _watermark(db) == settled[-1]


@pytest.mark.asyncio
async def test_a_later_completed_turn_passes_the_unfinished_one_with_a_marker(
    db_engine: AsyncEngine,
) -> None:
    limits = review_limits()
    db = memory_db(db_engine, limits)
    script = CuratorScript()
    service = curator_service(db_engine, curator_llm(script))

    await enable_contribution(db)
    await seed_turn(db, turn_id="turn-1", said="about the holiday --", replied=None)
    finished = await seed_turn(
        db,
        turn_id="turn-2",
        said="we always take the tram",
        at=NOW - timedelta(minutes=40),
    )

    await _review(db, service, limits=limits)

    assert UNFINISHED_TURN_MARKER in script.requests[0]
    assert "about the holiday" in script.requests[0]
    assert await _watermark(db) == finished[-1]


@pytest.mark.asyncio
async def test_a_long_stretch_is_reviewed_across_successive_sweeps(
    db_engine: AsyncEngine,
) -> None:
    """More rows than one review may read leave the conversation still due."""
    limits = review_limits(review_input_max_chars=900)
    db = memory_db(db_engine, limits)
    script = CuratorScript()
    service = curator_service(db_engine, curator_llm(script))

    await enable_contribution(db)
    for index in range(6):
        await seed_turn(
            db,
            turn_id=f"turn-{index}",
            said=f"turn {index}: " + "we talked about the holiday. " * 8,
            at=NOW - timedelta(minutes=60 - index),
        )

    first = await _review(db, service, limits=limits)
    still_due = await select_review_rows(
        db,
        interface_type=WEB,
        conversation_id=CONVERSATION,
        watermark=await _watermark(db),
        settings=SETTINGS,
        contributing_profiles={CONTRIBUTOR: ENABLED_AT},
    )
    second = await _review(db, service, limits=limits)

    assert first is MemoryReviewResult.APPLIED
    assert still_due, "the leftover rows should keep the conversation due"
    assert second is MemoryReviewResult.APPLIED
    assert "turn 0:" in script.requests[0]
    assert "turn 0:" not in script.requests[1]


@pytest.mark.asyncio
async def test_leftover_rows_keep_the_conversation_due_for_the_next_sweep(
    db_engine: AsyncEngine,
) -> None:
    limits = review_limits(review_input_max_chars=900)
    db = memory_db(db_engine, limits)
    service = curator_service(db_engine, curator_llm(CuratorScript()))

    await enable_contribution(db)
    for index in range(6):
        await seed_turn(
            db,
            turn_id=f"turn-{index}",
            said=f"turn {index}: " + "we talked about the holiday. " * 8,
            at=NOW - timedelta(minutes=60 - index),
        )
    await _review(db, service, limits=limits)
    await db.tasks.delete_finished(memory_review_task_id(WEB, CONVERSATION))

    enqueued = await run_memory_review_sweep(
        _context(db, service),
        settings=SETTINGS,
        configured_contributors={CONTRIBUTOR},
    )

    assert enqueued == 1


# ---------------------------------------------------------------------------
# Outcomes other than applied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stretch_nobody_spoke_in_is_skipped(db_engine: AsyncEngine) -> None:
    """An application-written trigger is not a person speaking.

    A callback or automation turn leaves a ``role='user'`` row the application
    wrote. The design skips such a stretch rather than spending a model call on
    it, and advances past it so it is not looked at again.
    """
    limits = review_limits()
    db = memory_db(db_engine, limits)
    llm = curator_llm(CuratorScript())
    service = curator_service(db_engine, llm)

    await enable_contribution(db)
    ids = await seed_turn(
        db,
        turn_id="turn-1",
        said="System Callback Trigger",
        replied="Reminder sent.",
        internal=True,
    )

    result = await _review(db, service, limits=limits)

    assert result is MemoryReviewResult.SKIPPED
    assert llm.get_calls() == []
    assert await _watermark(db) == ids[-1]
    assert [row.outcome for row in await _outcomes(db)] == ["skipped"]


@pytest.mark.asyncio
async def test_a_curator_that_proposes_nothing_records_no_changes(
    db_engine: AsyncEngine,
) -> None:
    limits = review_limits()
    db = memory_db(db_engine, limits)
    service = curator_service(db_engine, curator_llm(CuratorScript(propose=False)))

    await enable_contribution(db)
    ids = await seed_turn(db, turn_id="turn-1", said="what time is it")

    result = await _review(db, service, limits=limits)

    assert result is MemoryReviewResult.NO_CHANGES
    assert await _watermark(db) == ids[-1]
    assert [row.outcome for row in await _outcomes(db)] == ["no_changes"]


@pytest.mark.asyncio
async def test_a_refused_proposal_gets_one_retry_and_then_the_review_ends(
    db_engine: AsyncEngine,
) -> None:
    """The budget is the tool's, not the prompt's.

    The fake curator proposes the same out-of-scope citation for as long as it
    is invited to. Two lists are refused with reasons; the third is refused
    outright, and the review is abandoned with its watermark advanced.
    """
    limits = review_limits()
    db = memory_db(db_engine, limits)
    script = CuratorScript(keep_proposing=True, cite_out_of_scope=True)
    llm = curator_llm(script)
    service = curator_service(db_engine, llm)

    await enable_contribution(db)
    ids = await seed_turn(db, turn_id="turn-1", said="we always take the tram")

    result = await _review(db, service, limits=limits)

    results = tool_results(llm.get_calls()[-1]["kwargs"]["messages"])
    assert result is MemoryReviewResult.ABANDONED
    assert sum("Every edit is refused together" in r for r in results) == 2, (
        "one proposal and one retry are refused with reasons"
    )
    assert sum("will not look at another proposal" in r for r in results) == 1, (
        "and a third is refused outright"
    )
    assert await _watermark(db) == ids[-1]
    outcome = (await _outcomes(db))[0]
    assert outcome.outcome == "abandoned"
    assert outcome.reason is not None
    assert "refused" in outcome.reason


@pytest.mark.asyncio
async def test_the_rejection_reasons_reach_the_model(db_engine: AsyncEngine) -> None:
    limits = review_limits()
    db = memory_db(db_engine, limits)
    script = CuratorScript(keep_proposing=True, cite_out_of_scope=True)
    llm = curator_llm(script)
    service = curator_service(db_engine, llm)

    await enable_contribution(db)
    await seed_turn(db, turn_id="turn-1", said="we always take the tram")

    await _review(db, service, limits=limits)

    seen = "\n".join(str(call["kwargs"]["messages"]) for call in llm.get_calls())
    assert "Cite only messages you were shown" in seen


# ---------------------------------------------------------------------------
# Serialisation and conflict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_due_conversations_apply_in_sequence(db_engine: AsyncEngine) -> None:
    """The second review is shown what the first one wrote."""
    limits = review_limits()
    db = memory_db(db_engine, limits)
    script = CuratorScript()
    service = curator_service(db_engine, curator_llm(script))

    await enable_contribution(db)
    await seed_turn(db, turn_id="turn-a", said="we always take the tram")
    await seed_turn(
        db,
        turn_id="turn-b",
        said="Sam starts swimming on Tuesdays",
        conversation_id="second-conv",
        at=NOW - timedelta(minutes=40),
    )

    first = await _review(db, service, limits=limits)
    second = await _review(db, service, limits=limits, conversation_id="second-conv")

    assert first is MemoryReviewResult.APPLIED
    assert second is MemoryReviewResult.APPLIED
    assert script.entry in script.requests[1], (
        "the second review should see the first one's entry among the current "
        "memory topics"
    )


@pytest.mark.asyncio
async def test_a_persons_edit_during_a_review_makes_it_re_run(
    db_engine: AsyncEngine,
) -> None:
    """A conflicting apply is re-run against the store the person left behind.

    The gate holds the curator's first reply in flight; the person's edit lands
    while it is held, so the apply's compare-and-set fails on a revision that
    moved rather than on anything wrong with the edits.
    """
    limits = review_limits()
    db = memory_db(db_engine, limits)
    gate = asyncio.Event()
    script = CuratorScript()
    llm = curator_llm(script)
    llm.response_gate = gate
    service = curator_service(db_engine, llm)

    await enable_contribution(db)
    await seed_turn(db, turn_id="turn-1", said="we always take the tram")

    review = asyncio.create_task(_review(db, service, limits=limits))
    await wait_for_condition(
        lambda: _curator_turn_started(db_engine),
        description="the curator's turn to reach its model call",
    )
    await db.notes.add_or_update(
        title="Alice",
        content="- Alice corrected this by hand while the review ran.",
        include_in_prompt=False,
        visibility_labels=[MEMORY_LABEL],
        write_policy=PERSON_WRITE_POLICY,
        provenance=NoteProvenanceStamp.internal(),
    )
    gate.set()
    result = await review

    assert result is MemoryReviewResult.APPLIED
    assert len(script.requests) == 2
    assert "corrected this by hand" in script.requests[1]
    assert "corrected this by hand" in await _note(db, "Alice")


async def _curator_trigger_row(db: Database) -> MessageHistoryRow | None:
    """The curator's own request row, if a review has written one.

    Found by its subconversation rather than through any history API: the row
    is deliberately internal, so every user-facing read is meant to miss it.
    """
    rows = await db.message_history.rows_matching(
        sa.and_(
            message_history_table.c.conversation_id == CONVERSATION,
            message_history_table.c.subconversation_id.isnot(None),
            message_history_table.c.role == "user",
        )
    )
    return rows[0] if rows else None


async def _curator_turn_started(engine: AsyncEngine) -> bool:
    """Whether the review has reached its model call.

    The trigger row is persisted before the call, so it is the observable
    moment between "the review has read the store revision" and "the review has
    proposed against it".
    """
    return await _curator_trigger_row(Database(engine=engine)) is not None


# ---------------------------------------------------------------------------
# The curator's own rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_curators_rows_stay_out_of_the_conversation_list(
    db_engine: AsyncEngine,
) -> None:
    limits = review_limits()
    db = memory_db(db_engine, limits)
    service = curator_service(db_engine, curator_llm(CuratorScript()))
    await enable_contribution(db)
    await seed_turn(db, turn_id="turn-1", said="we always take the tram")
    await _review(db, service, limits=limits)

    summaries, _ = await db.message_history.get_conversation_summaries(
        include_subconversations=False
    )

    assert [summary["conversation_id"] for summary in summaries] == [CONVERSATION]
    assert summaries[0]["message_count"] == 2


@pytest.mark.asyncio
async def test_the_curators_rows_are_not_in_the_source_conversations_history(
    db_engine: AsyncEngine,
) -> None:
    limits = review_limits()
    db = memory_db(db_engine, limits)
    service = curator_service(db_engine, curator_llm(CuratorScript()))
    await enable_contribution(db)
    await seed_turn(db, turn_id="turn-1", said="we always take the tram")
    await _review(db, service, limits=limits)

    rows, _, _ = await db.message_history.get_conversation_messages_paginated(
        CONVERSATION, include_subconversations=False
    )

    ordered = sorted(rows, key=lambda row: row["internal_id"])
    assert [row["content"] for row in ordered] == ["we always take the tram", "Noted."]


@pytest.mark.asyncio
async def test_the_curators_rows_are_never_eligible_for_review(
    db_engine: AsyncEngine,
) -> None:
    """Otherwise a review would learn from the last review's own reasoning."""
    limits = review_limits()
    db = memory_db(db_engine, limits)
    service = curator_service(db_engine, curator_llm(CuratorScript()))
    await enable_contribution(db)
    await seed_turn(db, turn_id="turn-1", said="we always take the tram")
    await _review(db, service, limits=limits)

    rows = await select_review_rows(
        db,
        interface_type=WEB,
        conversation_id=CONVERSATION,
        watermark=NO_WATERMARK,
        settings=SETTINGS,
        contributing_profiles={CONTRIBUTOR: ENABLED_AT},
    )

    assert all(row["subconversation_id"] is None for row in rows)
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_a_trusted_stretch_leaves_the_curators_turn_in_the_trusted_pole(
    db_engine: AsyncEngine,
) -> None:
    """The review seeds the curator's turn from the chunk's merged taint.

    A trusted stretch therefore produces a turn inside the trusted pole, which
    is what the notes repository requires of every memory write. The written
    note is stamped ``trusted_internal``: the curator composed it, so it is
    never the human's own words.
    """
    limits = review_limits()
    db = memory_db(db_engine, limits)
    service = curator_service(db_engine, curator_llm(CuratorScript()))
    await enable_contribution(db)
    await seed_turn(db, turn_id="turn-1", said="we always take the tram")

    await _review(db, service, limits=limits)

    trigger = await _curator_trigger_row(db)
    assert trigger is not None
    assert not is_externally_authored(
        TurnTaintState.from_metadata(trigger["taint_metadata"]).max_tier
    )
    written = await db.notes.get_by_title("Sam", read_policy=CURATOR_READ_POLICY)
    assert written is not None
    assert written.provenance_metadata is not None
    assert (
        TurnTaintState.from_metadata(
            written.provenance_metadata.get("taint_metadata")
        ).max_tier
        is SourceTrustTier.TRUSTED_INTERNAL
    )


@pytest.mark.asyncio
async def test_a_review_that_finds_nothing_due_records_nothing(
    db_engine: AsyncEngine,
) -> None:
    """A stale review task is not an outcome; it is a task that lost its race."""
    limits = review_limits()
    db = memory_db(db_engine, limits)
    llm = curator_llm(CuratorScript())
    service = curator_service(db_engine, llm)
    await enable_contribution(db)

    result = await _review(db, service, limits=limits)

    assert result is MemoryReviewResult.DEFERRED
    assert await _outcomes(db) == []
    assert llm.get_calls() == []


@pytest.mark.asyncio
async def test_the_review_task_runs_under_the_background_lane(
    db_engine: AsyncEngine,
) -> None:
    db = memory_db(db_engine, review_limits())
    service = curator_service(db_engine, curator_llm(CuratorScript()))
    await enable_contribution(db)
    await seed_turn(db, turn_id="turn-1", said="we always take the tram")

    await run_memory_review_sweep(
        _context(db, service),
        settings=SETTINGS,
        configured_contributors={CONTRIBUTOR},
    )

    tasks = await db.tasks.get_all(task_type=MEMORY_REVIEW_TASK_TYPE)
    assert [task["priority"] for task in tasks] == [TaskPriority.BACKGROUND]
