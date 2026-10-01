"""Worker completion listeners whose worker can no longer report back."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import update

from family_assistant.plugins.ai_workers.tasks import handle_worker_task_cleanup
from family_assistant.storage.database import Database
from family_assistant.storage.events import (
    WORKER_COMPLETION_EVENT_TYPE,
    event_listeners_table,
)
from family_assistant.storage.repositories.worker_tasks import worker_tasks_table
from family_assistant.tools import ToolExecutionContext

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncEngine

CONVERSATION_ID = "test-conv-123"


@pytest.fixture
async def db_context(db_engine: AsyncEngine) -> AsyncGenerator[Database]:
    """Create a database context for testing."""
    context = Database(engine=db_engine)
    yield context


@pytest.fixture
def exec_context(db_context: Database) -> ToolExecutionContext:
    """Build the real execution context the task worker hands a handler.

    Constructed rather than faked so that a dependency added to the handler
    fails here instead of being silently absent.
    """
    return ToolExecutionContext(
        interface_type="web",
        conversation_id=CONVERSATION_ID,
        user_name="test_user",
        turn_id=None,
        db_context=db_context,
        processing_service=None,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        credential_resolvers=None,
        api_backend=None,
        timezone=ZoneInfo("UTC"),
    )


async def _create_worker_completion_listener(
    db_context: Database,
    task_id: str,
    *,
    age: timedelta,
) -> int:
    """Arm a worker completion listener the way spawn_worker does."""
    listener_id = await db_context.events.create_event_listener(
        name=f"worker-{task_id}-completion",
        source_id="webhook",
        match_conditions={
            "event_type": WORKER_COMPLETION_EVENT_TYPE,
            "data.task_id": task_id,
        },
        conversation_id=CONVERSATION_ID,
        one_time=True,
        enabled=True,
    )
    stmt = (
        update(event_listeners_table)
        .where(event_listeners_table.c.id == listener_id)
        .values(created_at=datetime.now(UTC) - age)
    )
    await db_context.execute(stmt)
    return listener_id


async def _create_worker_task(
    db_context: Database,
    task_id: str,
    status: str,
    *,
    age: timedelta = timedelta(0),
    finished_age: timedelta | None = None,
    timeout_minutes: int = 30,
) -> None:
    await db_context.worker_tasks.create_task(
        task_id=task_id,
        conversation_id=CONVERSATION_ID,
        interface_type="test",
        task_description="do a thing",
        timeout_minutes=timeout_minutes,
    )
    if status != "pending":
        await db_context.worker_tasks.update_task_status(task_id=task_id, status=status)
    if age:
        aged = datetime.now(UTC) - age
        values: dict[str, datetime] = {"created_at": aged}
        if finished_age is not None:
            values["completed_at"] = datetime.now(UTC) - finished_age
        await db_context.execute(
            update(worker_tasks_table)
            .where(worker_tasks_table.c.task_id == task_id)
            .values(**values)
        )


class TestWorkerCompletionListenerCleanup:
    """The worker task cleanup collects listeners no worker will fire."""

    @pytest.mark.asyncio
    async def test_deletes_listener_for_terminal_worker(
        self, exec_context: ToolExecutionContext, db_context: Database
    ) -> None:
        """A worker that failed will never send its completion webhook."""
        await _create_worker_task(
            db_context,
            "task-failed",
            "failed",
            age=timedelta(days=2),
            finished_age=timedelta(days=2),
        )
        listener_id = await _create_worker_completion_listener(
            db_context, "task-failed", age=timedelta(days=2)
        )

        await handle_worker_task_cleanup(exec_context, {})

        assert await db_context.events.get_event_listener_by_id(listener_id) is None

    @pytest.mark.asyncio
    async def test_deletes_listener_whose_worker_task_is_gone(
        self, exec_context: ToolExecutionContext, db_context: Database
    ) -> None:
        """The worker task cleanup reaps rows well before listeners expire."""
        listener_id = await _create_worker_completion_listener(
            db_context, "task-reaped", age=timedelta(days=8)
        )

        await handle_worker_task_cleanup(exec_context, {})

        assert await db_context.events.get_event_listener_by_id(listener_id) is None

    @pytest.mark.asyncio
    async def test_preserves_recent_listener_whose_worker_task_is_gone(
        self, exec_context: ToolExecutionContext, db_context: Database
    ) -> None:
        """A missing row is not proof the completion has already been acted on.

        The worker task cleanup deletes terminal rows by age, so a task that
        finished as its row was reaped leaves nothing to read. Waiting out the
        week covers that window, which is shorter than the row retention.
        """
        listener_id = await _create_worker_completion_listener(
            db_context, "task-recently-reaped", age=timedelta(days=2)
        )

        await handle_worker_task_cleanup(exec_context, {})

        assert await db_context.events.get_event_listener_by_id(listener_id) is not None

    @pytest.mark.asyncio
    async def test_preserves_listener_for_running_worker(
        self, exec_context: ToolExecutionContext, db_context: Database
    ) -> None:
        """A worker still inside its own timeout is going to report back."""
        await _create_worker_task(db_context, "task-running", "running")
        listener_id = await _create_worker_completion_listener(
            db_context, "task-running", age=timedelta(days=2)
        )

        await handle_worker_task_cleanup(exec_context, {})

        assert await db_context.events.get_event_listener_by_id(listener_id) is not None

    @pytest.mark.asyncio
    async def test_deletes_abandoned_listener_for_running_worker(
        self, exec_context: ToolExecutionContext, db_context: Database
    ) -> None:
        """A week on, a worker overdue by its own timeout has lost its job."""
        await _create_worker_task(
            db_context, "task-stuck", "running", age=timedelta(days=8)
        )
        listener_id = await _create_worker_completion_listener(
            db_context, "task-stuck", age=timedelta(days=8)
        )

        await handle_worker_task_cleanup(exec_context, {})

        assert await db_context.events.get_event_listener_by_id(listener_id) is None

    @pytest.mark.asyncio
    async def test_preserves_listener_for_worker_still_within_its_timeout(
        self, exec_context: ToolExecutionContext, db_context: Database
    ) -> None:
        """A long timeout an operator allowed outranks the week-old rule."""
        await _create_worker_task(
            db_context,
            "task-fortnight",
            "running",
            age=timedelta(days=8),
            timeout_minutes=14 * 24 * 60,
        )
        listener_id = await _create_worker_completion_listener(
            db_context, "task-fortnight", age=timedelta(days=8)
        )

        await handle_worker_task_cleanup(exec_context, {})

        assert await db_context.events.get_event_listener_by_id(listener_id) is not None

    @pytest.mark.asyncio
    async def test_preserves_recent_listener_for_terminal_worker(
        self, exec_context: ToolExecutionContext, db_context: Database
    ) -> None:
        """Within the grace period the completion may still be in flight."""
        await _create_worker_task(db_context, "task-just-failed", "failed")
        listener_id = await _create_worker_completion_listener(
            db_context, "task-just-failed", age=timedelta(hours=1)
        )

        await handle_worker_task_cleanup(exec_context, {})

        assert await db_context.events.get_event_listener_by_id(listener_id) is not None

    @pytest.mark.asyncio
    async def test_preserves_listener_for_a_just_finished_worker(
        self, exec_context: ToolExecutionContext, db_context: Database
    ) -> None:
        """A completion marks the task terminal before it fires the listener.

        Collecting on terminal status alone would race that window and drop
        the wake for a worker that finished perfectly well.
        """
        await _create_worker_task(
            db_context, "task-just-landed", "success", age=timedelta(days=9)
        )
        listener_id = await _create_worker_completion_listener(
            db_context, "task-just-landed", age=timedelta(days=9)
        )

        await handle_worker_task_cleanup(exec_context, {})

        assert await db_context.events.get_event_listener_by_id(listener_id) is not None

    @pytest.mark.asyncio
    async def test_preserves_unrelated_one_time_listeners(
        self, exec_context: ToolExecutionContext, db_context: Database
    ) -> None:
        """Listeners waiting on something other than a worker are untouched."""
        listener_id = await db_context.events.create_event_listener(
            name="front-door-opens",
            source_id="home_assistant",
            match_conditions={"entity_id": "binary_sensor.front_door"},
            conversation_id=CONVERSATION_ID,
            one_time=True,
            enabled=True,
        )
        stmt = (
            update(event_listeners_table)
            .where(event_listeners_table.c.id == listener_id)
            .values(created_at=datetime.now(UTC) - timedelta(days=30))
        )
        await db_context.execute(stmt)

        await handle_worker_task_cleanup(exec_context, {})

        assert await db_context.events.get_event_listener_by_id(listener_id) is not None

    @pytest.mark.asyncio
    async def test_preserves_listener_that_already_fired(
        self, exec_context: ToolExecutionContext, db_context: Database
    ) -> None:
        """Fired listeners belong to the completed automation cleanup."""
        await _create_worker_task(
            db_context,
            "task-done",
            "success",
            age=timedelta(days=2),
            finished_age=timedelta(days=2),
        )
        listener_id = await _create_worker_completion_listener(
            db_context, "task-done", age=timedelta(days=2)
        )
        stmt = (
            update(event_listeners_table)
            .where(event_listeners_table.c.id == listener_id)
            .values(enabled=False, last_execution_at=datetime.now(UTC))
        )
        await db_context.execute(stmt)

        await handle_worker_task_cleanup(exec_context, {})

        assert await db_context.events.get_event_listener_by_id(listener_id) is not None

    @pytest.mark.asyncio
    async def test_honours_payload_overrides(
        self, exec_context: ToolExecutionContext, db_context: Database
    ) -> None:
        """Grace periods come from the payload when it supplies them."""
        await _create_worker_task(
            db_context,
            "task-tuned",
            "failed",
            age=timedelta(hours=2),
            finished_age=timedelta(hours=2),
        )
        listener_id = await _create_worker_completion_listener(
            db_context, "task-tuned", age=timedelta(hours=2)
        )

        await handle_worker_task_cleanup(
            exec_context,
            {"dead_worker_grace_hours": 1},
        )

        assert await db_context.events.get_event_listener_by_id(listener_id) is None
