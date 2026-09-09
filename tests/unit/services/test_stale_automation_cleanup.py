"""Tests for the stale automation cleanup handler."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import update

from family_assistant.storage.database import Database
from family_assistant.storage.events import (
    WORKER_COMPLETION_EVENT_TYPE,
    event_listeners_table,
)
from family_assistant.storage.repositories.worker_tasks import worker_tasks_table
from family_assistant.storage.schedule_automations import schedule_automations_table
from family_assistant.storage.tasks import tasks_table
from family_assistant.task_worker import handle_stale_automation_cleanup
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
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
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
    """Worker completion listeners whose worker can no longer report back."""

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

        await handle_stale_automation_cleanup(exec_context, {})

        assert await db_context.events.get_event_listener_by_id(listener_id) is None

    @pytest.mark.asyncio
    async def test_deletes_listener_whose_worker_task_is_gone(
        self, exec_context: ToolExecutionContext, db_context: Database
    ) -> None:
        """The worker task cleanup reaps rows well before listeners expire."""
        listener_id = await _create_worker_completion_listener(
            db_context, "task-reaped", age=timedelta(days=8)
        )

        await handle_stale_automation_cleanup(exec_context, {})

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

        await handle_stale_automation_cleanup(exec_context, {})

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

        await handle_stale_automation_cleanup(exec_context, {})

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

        await handle_stale_automation_cleanup(exec_context, {})

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

        await handle_stale_automation_cleanup(exec_context, {})

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

        await handle_stale_automation_cleanup(exec_context, {})

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

        await handle_stale_automation_cleanup(exec_context, {})

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

        await handle_stale_automation_cleanup(exec_context, {})

        assert await db_context.events.get_event_listener_by_id(listener_id) is not None

    @pytest.mark.asyncio
    async def test_preserves_listener_that_already_fired(
        self, exec_context: ToolExecutionContext, db_context: Database
    ) -> None:
        """Fired listeners belong to the completed automation cleanup."""
        await _create_worker_task(db_context, "task-done", "success")
        listener_id = await _create_worker_completion_listener(
            db_context, "task-done", age=timedelta(days=2)
        )
        stmt = (
            update(event_listeners_table)
            .where(event_listeners_table.c.id == listener_id)
            .values(enabled=False, last_execution_at=datetime.now(UTC))
        )
        await db_context.execute(stmt)

        await handle_stale_automation_cleanup(exec_context, {})

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

        await handle_stale_automation_cleanup(
            exec_context,
            {"dead_worker_grace_hours": 1},
        )

        assert await db_context.events.get_event_listener_by_id(listener_id) is None


class TestSpentScheduleAutomationCleanup:
    """Schedule automations whose recurrence rule has nothing left."""

    async def _create_spent_schedule(
        self,
        db_context: Database,
        name: str,
        *,
        recurrence_rule: str,
        next_scheduled_at: datetime,
        clear_pending_tasks: bool = True,
    ) -> int:
        """Create an automation and leave it in the state a final run leaves.

        Creation rejects a rule with no occurrence ahead of it, so the spent
        rule is written afterwards, alongside the next_scheduled_at that the
        last firing froze and the consumed task it enqueued.
        """
        automation_id = await db_context.schedule_automations.create(
            name=name,
            conversation_id=CONVERSATION_ID,
            recurrence_rule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            action_type="wake_llm",
            action_config={"context": "check on it"},
            interface_type="web",
            timezone=ZoneInfo("UTC"),
        )
        await db_context.execute(
            update(schedule_automations_table)
            .where(schedule_automations_table.c.id == automation_id)
            .values(
                recurrence_rule=recurrence_rule,
                next_scheduled_at=next_scheduled_at,
            )
        )
        if clear_pending_tasks:
            await db_context.execute(
                update(tasks_table)
                .where(tasks_table.c.status == "pending")
                .where(
                    tasks_table.c.payload["automation_id"].as_string()
                    == str(automation_id)
                )
                .values(status="completed")
            )
        return automation_id

    @pytest.mark.asyncio
    async def test_deletes_exhausted_schedule(
        self, exec_context: ToolExecutionContext, db_context: Database
    ) -> None:
        """A rule bounded by a past UNTIL can never produce another run."""
        automation_id = await self._create_spent_schedule(
            db_context,
            "spent-schedule",
            recurrence_rule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0;UNTIL=20200101T000000Z",
            next_scheduled_at=datetime.now(UTC) - timedelta(days=3),
        )

        await handle_stale_automation_cleanup(exec_context, {})

        assert (
            await db_context.schedule_automations.get_by_id(
                automation_id, CONVERSATION_ID
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_deletes_count_exhausted_schedule(
        self, exec_context: ToolExecutionContext, db_context: Database
    ) -> None:
        """A COUNT-bounded rule is spent as of the firing that consumed it.

        Evaluated from now instead, such a rule reports a fresh occurrence
        today and the automation would never be collected.
        """
        # next_scheduled_at is always an occurrence of the rule, so the
        # anchor has to be one here too for the fixture to be realistic.
        last_firing = (datetime.now(UTC) - timedelta(days=3)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        automation_id = await self._create_spent_schedule(
            db_context,
            "count-exhausted-schedule",
            recurrence_rule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0;COUNT=1",
            next_scheduled_at=last_firing,
        )

        await handle_stale_automation_cleanup(exec_context, {})

        assert (
            await db_context.schedule_automations.get_by_id(
                automation_id, CONVERSATION_ID
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_preserves_schedule_with_unparseable_rule(
        self, exec_context: ToolExecutionContext, db_context: Database
    ) -> None:
        """A rule that does not parse is broken, not spent."""
        automation_id = await self._create_spent_schedule(
            db_context,
            "broken-schedule",
            recurrence_rule="FREQ=EVERY_OTHER_TUESDAY",
            next_scheduled_at=datetime.now(UTC) - timedelta(days=3),
        )

        await handle_stale_automation_cleanup(exec_context, {})

        assert (
            await db_context.schedule_automations.get_by_id(
                automation_id, CONVERSATION_ID
            )
            is not None
        )

    @pytest.mark.asyncio
    async def test_deletes_schedule_that_ran_late_past_its_end_date(
        self, exec_context: ToolExecutionContext, db_context: Database
    ) -> None:
        """A late run leaves next_scheduled_at frozen before the end date.

        The series then still has occurrences between that anchor and the end
        date, all of them in the past. Reading those as life would keep a
        schedule that can never fire again.
        """
        end_date = datetime.now(UTC) - timedelta(days=4)
        frozen = datetime.now(UTC) - timedelta(days=6)
        automation_id = await self._create_spent_schedule(
            db_context,
            "late-then-expired-schedule",
            recurrence_rule=(
                f"FREQ=DAILY;BYHOUR=9;BYMINUTE=0;UNTIL={end_date:%Y%m%dT%H%M%SZ}"
            ),
            next_scheduled_at=frozen,
        )

        await handle_stale_automation_cleanup(exec_context, {})

        assert (
            await db_context.schedule_automations.get_by_id(
                automation_id, CONVERSATION_ID
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_preserves_recurring_schedule_that_is_behind(
        self, exec_context: ToolExecutionContext, db_context: Database
    ) -> None:
        """A stranded but still-recurring automation is not ours to delete."""
        automation_id = await self._create_spent_schedule(
            db_context,
            "daily-schedule",
            recurrence_rule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            next_scheduled_at=datetime.now(UTC) - timedelta(days=3),
        )

        await handle_stale_automation_cleanup(exec_context, {})

        assert (
            await db_context.schedule_automations.get_by_id(
                automation_id, CONVERSATION_ID
            )
            is not None
        )

    @pytest.mark.asyncio
    async def test_preserves_recently_spent_schedule(
        self, exec_context: ToolExecutionContext, db_context: Database
    ) -> None:
        """Inside the grace period the final firing may still be running."""
        automation_id = await self._create_spent_schedule(
            db_context,
            "just-spent-schedule",
            recurrence_rule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0;UNTIL=20200101T000000Z",
            next_scheduled_at=datetime.now(UTC) - timedelta(hours=1),
        )

        await handle_stale_automation_cleanup(exec_context, {})

        assert (
            await db_context.schedule_automations.get_by_id(
                automation_id, CONVERSATION_ID
            )
            is not None
        )

    @pytest.mark.asyncio
    async def test_preserves_spent_schedule_with_a_pending_task(
        self, exec_context: ToolExecutionContext, db_context: Database
    ) -> None:
        """A queued run still owed to the user outranks the rule being spent."""
        automation_id = await self._create_spent_schedule(
            db_context,
            "spent-with-pending-task",
            recurrence_rule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0;UNTIL=20200101T000000Z",
            next_scheduled_at=datetime.now(UTC) - timedelta(days=3),
            clear_pending_tasks=False,
        )

        await handle_stale_automation_cleanup(exec_context, {})

        assert (
            await db_context.schedule_automations.get_by_id(
                automation_id, CONVERSATION_ID
            )
            is not None
        )

    @pytest.mark.asyncio
    async def test_preserves_disabled_spent_schedule(
        self, exec_context: ToolExecutionContext, db_context: Database
    ) -> None:
        """Disabled automations are the user's to re-enable, rule and all."""
        automation_id = await self._create_spent_schedule(
            db_context,
            "disabled-spent-schedule",
            recurrence_rule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0;UNTIL=20200101T000000Z",
            next_scheduled_at=datetime.now(UTC) - timedelta(days=3),
        )
        await db_context.execute(
            update(schedule_automations_table)
            .where(schedule_automations_table.c.id == automation_id)
            .values(enabled=False)
        )

        await handle_stale_automation_cleanup(exec_context, {})

        assert (
            await db_context.schedule_automations.get_by_id(
                automation_id, CONVERSATION_ID
            )
            is not None
        )
