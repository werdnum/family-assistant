"""Tests for the order in which the queue hands out eligible tasks."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.database import Database

TASK_TYPE = "ordering_probe"


@pytest.mark.asyncio
async def test_task_due_earlier_wins_over_later_immediate_task(
    db_engine: AsyncEngine,
) -> None:
    """A scheduled task that came due first is dequeued before newer immediate work."""
    db = Database(db_engine)
    now = datetime.now(UTC)

    await db.tasks.enqueue(
        task_id="due_ten_minutes_ago",
        task_type=TASK_TYPE,
        scheduled_at=now - timedelta(minutes=10),
    )
    await db.tasks.enqueue(task_id="just_created", task_type=TASK_TYPE)

    task = await db.tasks.dequeue(
        worker_id="worker",
        task_types=[TASK_TYPE],
        current_time=now,
    )

    assert task is not None
    assert task["task_id"] == "due_ten_minutes_ago"


@pytest.mark.asyncio
async def test_retry_whose_backoff_elapsed_is_not_demoted(
    db_engine: AsyncEngine,
) -> None:
    """A retry that is due again runs before immediate work created after it."""
    db = Database(db_engine)
    now = datetime.now(UTC)

    await db.tasks.enqueue(task_id="retried", task_type=TASK_TYPE)
    assert await db.tasks.reschedule_for_retry(
        task_id="retried",
        next_scheduled_at=now - timedelta(minutes=1),
        new_retry_count=1,
        error="transient failure",
    )
    await db.tasks.enqueue(task_id="fresh", task_type=TASK_TYPE)

    task = await db.tasks.dequeue(
        worker_id="worker",
        task_types=[TASK_TYPE],
        current_time=now,
    )

    assert task is not None
    assert task["task_id"] == "retried"


@pytest.mark.asyncio
async def test_task_scheduled_for_the_future_is_not_dequeued(
    db_engine: AsyncEngine,
) -> None:
    """Due-time ordering does not make an ineligible task eligible."""
    db = Database(db_engine)
    now = datetime.now(UTC)

    await db.tasks.enqueue(
        task_id="not_yet_due",
        task_type=TASK_TYPE,
        scheduled_at=now + timedelta(minutes=5),
    )

    task = await db.tasks.dequeue(
        worker_id="worker",
        task_types=[TASK_TYPE],
        current_time=now,
    )

    assert task is None


@pytest.mark.asyncio
async def test_get_all_ascending_orders_by_due_time(db_engine: AsyncEngine) -> None:
    """The admin listing shows tasks in the order the queue will run them."""
    db = Database(db_engine)
    now = datetime.now(UTC)

    await db.tasks.enqueue(
        task_id="due_earlier",
        task_type=TASK_TYPE,
        scheduled_at=now - timedelta(minutes=10),
    )
    await db.tasks.enqueue(task_id="immediate", task_type=TASK_TYPE)
    await db.tasks.enqueue(
        task_id="due_later",
        task_type=TASK_TYPE,
        scheduled_at=now + timedelta(minutes=10),
    )

    tasks = await db.tasks.get_all(task_type=TASK_TYPE)

    assert [task["task_id"] for task in tasks] == [
        "due_earlier",
        "immediate",
        "due_later",
    ]


@pytest.mark.asyncio
async def test_queue_state_snapshot_classifies_every_state(
    db_engine: AsyncEngine,
) -> None:
    """Each row lands in the state the queue would treat it as being in."""
    db = Database(db_engine)
    now = datetime.now(UTC)

    await db.tasks.enqueue(
        task_id="scheduled",
        task_type=TASK_TYPE,
        scheduled_at=now + timedelta(minutes=30),
    )
    await db.tasks.enqueue(
        task_id="due",
        task_type=TASK_TYPE,
        scheduled_at=now - timedelta(minutes=4),
    )
    # Claimed through their own task types, so each dequeue takes the row it is
    # meant to and not whichever pending row happens to be due first.
    await db.tasks.enqueue(task_id="processing", task_type="claimed_probe")
    await db.tasks.dequeue(
        worker_id="worker",
        task_types=["claimed_probe"],
        current_time=now,
    )
    await db.tasks.enqueue(task_id="stalled", task_type="stalled_probe")
    await db.tasks.dequeue(
        worker_id="crashed_worker",
        task_types=["stalled_probe"],
        current_time=now - timedelta(minutes=20),
    )
    await db.tasks.enqueue(
        task_id="exhausted", task_type=TASK_TYPE, max_retries_override=1
    )
    await db.tasks.reschedule_for_retry(
        task_id="exhausted",
        next_scheduled_at=now - timedelta(minutes=1),
        new_retry_count=2,
        error="gave up",
    )
    await db.tasks.enqueue(task_id="finished", task_type=TASK_TYPE)
    await db.tasks.update_status(task_id="finished", status="done")

    snapshot = await db.tasks.queue_state_snapshot(now)

    assert (snapshot.scheduled, snapshot.due, snapshot.processing) == (1, 1, 1)
    assert (snapshot.stalled, snapshot.exhausted) == (1, 1)
    assert snapshot.due_latency_seconds == pytest.approx(240, abs=30)


@pytest.mark.asyncio
async def test_queue_state_snapshot_reports_no_latency_when_nothing_is_due(
    db_engine: AsyncEngine,
) -> None:
    """An empty queue reports zero rather than an absent or NaN latency."""
    db = Database(db_engine)
    now = datetime.now(UTC)

    await db.tasks.enqueue(
        task_id="not_yet_due",
        task_type=TASK_TYPE,
        scheduled_at=now + timedelta(hours=1),
    )

    snapshot = await db.tasks.queue_state_snapshot(now)

    assert snapshot.due == 0
    assert snapshot.due_latency_seconds == 0.0
