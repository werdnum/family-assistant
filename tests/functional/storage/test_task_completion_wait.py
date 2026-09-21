"""Task polling must observe failures even when a worker finishes during a poll."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import Delete, Insert, Select, Update, update
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql.elements import TextClause

from family_assistant.storage.database import Database, ExecuteResult
from family_assistant.storage.tasks import TaskPriority, tasks_table
from tests import helpers


async def test_task_failure_during_poll_is_not_reported_as_success(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = "failure-during-poll"
    db = Database(db_engine)
    await db.tasks.enqueue(
        task_id=task_id, task_type="test", payload={}, priority=TaskPriority.INTERACTIVE
    )
    original_execute = Database.execute
    transitioned = False

    async def execute_then_fail_task(
        database: Database,
        query: Select | Insert | Update | Delete | TextClause,
        params: dict[str, object] | None = None,
    ) -> ExecuteResult:
        nonlocal transitioned
        result = await original_execute(database, query, params)
        if not transitioned:
            transitioned = True
            await original_execute(
                db,
                update(tasks_table)
                .where(tasks_table.c.task_id == task_id)
                .values(status="failed", error="worker failed during polling"),
            )
        return result

    # Finish the real task immediately after the poll's first database read.
    monkeypatch.setattr(Database, "execute", execute_then_fail_task)
    start_time = datetime.now(UTC)
    assert not await helpers._tasks_are_complete(
        db_engine, start_time, {task_id}, None, False
    )
    with pytest.raises(RuntimeError, match="worker failed during polling"):
        await helpers._tasks_are_complete(db_engine, start_time, {task_id}, None, False)
