"""The sweep that turns due conversations into review tasks.

Slice 4 of docs/design/conversation-memory.md: one review per due conversation,
keyed on the conversation so the same conversation never has two reviews in
flight. Dedup is the queue's -- a deterministic task id and a primary key --
rather than a check the sweep performs and could race.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from family_assistant.llm.messages import UserMessage
from family_assistant.memory.review_settings import MemoryReviewSettings
from family_assistant.memory.sweep import (
    MEMORY_REVIEW_TASK_TYPE,
    memory_review_task_id,
    run_memory_review_sweep,
)
from family_assistant.storage.database import Database
from family_assistant.tools.types import ToolExecutionContext
from family_assistant.utils.clock import MockClock

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.storage.types import TaskDict

NOW = datetime(2026, 9, 17, 18, 0, tzinfo=UTC)
ENABLED_AT = NOW - timedelta(days=7)
CONTRIBUTOR = "default_assistant"
CONVERSATION = "conv-1"

SETTINGS = MemoryReviewSettings(
    idle_window_minutes={"web": 30},
    contributing_interfaces=frozenset({"web"}),
)


def _context(db: Database, *, now: datetime = NOW) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="internal",
        conversation_id="sweep",
        user_name="system",
        turn_id=None,
        db_context=db,
        processing_service=None,
        clock=MockClock(now),
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        visibility_grants=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


async def _seed_due_conversation(
    db: Database, conversation_id: str = CONVERSATION
) -> None:
    """One idle conversation, under a profile that has been contributing."""
    await db.memory_review.record_enablement(
        profile_ids_contributing={CONTRIBUTOR}, now=ENABLED_AT
    )
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content="we always take the tram"),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=NOW - timedelta(minutes=45),
        turn_id="turn-1",
        processing_profile_id=CONTRIBUTOR,
        user_id="alice",
    )


async def _review_tasks(db: Database) -> list[TaskDict]:
    return await db.tasks.get_all(task_type=MEMORY_REVIEW_TASK_TYPE)


@pytest.mark.asyncio
async def test_a_due_conversation_is_enqueued_once(db_engine: AsyncEngine) -> None:
    db = Database(engine=db_engine)
    await _seed_due_conversation(db)

    enqueued = await run_memory_review_sweep(
        _context(db), settings=SETTINGS, configured_contributors={CONTRIBUTOR}
    )

    assert enqueued == 1
    tasks = await _review_tasks(db)
    assert [task["task_id"] for task in tasks] == [
        memory_review_task_id("web", CONVERSATION)
    ]
    assert tasks[0]["payload"] == {
        "interface_type": "web",
        "conversation_id": CONVERSATION,
    }


@pytest.mark.asyncio
async def test_two_sweeps_do_not_produce_two_reviews_in_flight(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _seed_due_conversation(db)
    await run_memory_review_sweep(
        _context(db), settings=SETTINGS, configured_contributors={CONTRIBUTOR}
    )

    enqueued = await run_memory_review_sweep(
        _context(db, now=NOW + timedelta(minutes=5)),
        settings=SETTINGS,
        configured_contributors={CONTRIBUTOR},
    )

    assert enqueued == 0
    assert len(await _review_tasks(db)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["done", "failed"])
async def test_a_finished_review_lets_the_next_sweep_enqueue_again(
    db_engine: AsyncEngine, terminal_status: str
) -> None:
    """A terminal row keeps its id reserved until the sweep clears it."""
    db = Database(engine=db_engine)
    await _seed_due_conversation(db)
    await run_memory_review_sweep(
        _context(db), settings=SETTINGS, configured_contributors={CONTRIBUTOR}
    )
    finished = (await _review_tasks(db))[0]
    await db.tasks.update_status(finished["task_id"], terminal_status)

    enqueued = await run_memory_review_sweep(
        _context(db, now=NOW + timedelta(minutes=5)),
        settings=SETTINGS,
        configured_contributors={CONTRIBUTOR},
    )

    assert enqueued == 1
    tasks = await _review_tasks(db)
    assert len(tasks) == 1
    assert tasks[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_two_due_conversations_get_one_review_each(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _seed_due_conversation(db, "conv-a")
    await _seed_due_conversation(db, "conv-b")

    enqueued = await run_memory_review_sweep(
        _context(db), settings=SETTINGS, configured_contributors={CONTRIBUTOR}
    )

    assert enqueued == 2
    assert {task["task_id"] for task in await _review_tasks(db)} == {
        memory_review_task_id("web", "conv-a"),
        memory_review_task_id("web", "conv-b"),
    }


@pytest.mark.asyncio
async def test_nothing_is_enqueued_when_memory_is_off(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _seed_due_conversation(db)
    settings = MemoryReviewSettings(
        enabled=False,
        idle_window_minutes=dict(SETTINGS.idle_window_minutes),
        contributing_interfaces=SETTINGS.contributing_interfaces,
    )

    enqueued = await run_memory_review_sweep(
        _context(db), settings=settings, configured_contributors={CONTRIBUTOR}
    )

    assert enqueued == 0
    assert await _review_tasks(db) == []


@pytest.mark.asyncio
async def test_nothing_is_enqueued_when_no_profile_is_configured_to_contribute(
    db_engine: AsyncEngine,
) -> None:
    """The shipped state: a sweep left behind by an earlier config does nothing."""
    db = Database(engine=db_engine)
    await _seed_due_conversation(db)

    enqueued = await run_memory_review_sweep(
        _context(db), settings=SETTINGS, configured_contributors=set()
    )

    assert enqueued == 0
    assert await _review_tasks(db) == []


@pytest.mark.asyncio
async def test_a_profile_configured_but_not_recorded_contributes_nothing(
    db_engine: AsyncEngine,
) -> None:
    """Stored enablement is the other half: no moment, no eligible rows."""
    db = Database(engine=db_engine)
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content="we always take the tram"),
        interface_type="web",
        conversation_id=CONVERSATION,
        timestamp=NOW - timedelta(minutes=45),
        turn_id="turn-1",
        processing_profile_id=CONTRIBUTOR,
        user_id="alice",
    )

    enqueued = await run_memory_review_sweep(
        _context(db), settings=SETTINGS, configured_contributors={CONTRIBUTOR}
    )

    assert enqueued == 0
    assert await _review_tasks(db) == []
