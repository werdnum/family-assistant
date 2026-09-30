"""Tests for the stale automation cleanup handler."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import update

from family_assistant.storage.database import Database
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
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        credential_resolvers=None,
        api_backend=None,
        timezone=ZoneInfo("UTC"),
    )


class TestSpentScheduleAutomationCleanup:
    """Schedule automations whose recurrence rule has nothing left."""

    async def _create_spent_schedule(
        self,
        db_context: Database,
        name: str,
        *,
        recurrence_rule: str,
        next_scheduled_at: datetime,
        recurrence_anchor: datetime | None = None,
        clear_pending_tasks: bool = True,
    ) -> int:
        """Create an automation and leave it in the state a final run leaves.

        Creation rejects a rule with no occurrence ahead of it, so the spent
        rule is written afterwards, alongside the next_scheduled_at that the
        last firing froze and the consumed task it enqueued. The anchor
        defaults to that last firing, which is where the scheduler leaves it
        for a rule without a COUNT, and where a single-occurrence series began.
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
                recurrence_anchor=recurrence_anchor or next_scheduled_at,
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
    async def test_deletes_schedule_whose_count_ran_out(
        self, exec_context: ToolExecutionContext, db_context: Database
    ) -> None:
        """A series is read from its anchor, so a COUNT above 1 also runs out.

        Read from the last firing instead, the count restarts there and the
        series never ends.
        """
        series_start = (datetime.now(UTC) - timedelta(days=5)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        automation_id = await self._create_spent_schedule(
            db_context,
            "count-ran-out-schedule",
            recurrence_rule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0;COUNT=3",
            next_scheduled_at=series_start + timedelta(days=2),
            recurrence_anchor=series_start,
        )

        await handle_stale_automation_cleanup(exec_context, {})

        assert (
            await db_context.schedule_automations.get_by_id(
                automation_id, CONVERSATION_ID
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_preserves_stranded_schedule_with_count_left(
        self, exec_context: ToolExecutionContext, db_context: Database
    ) -> None:
        """A COUNT series with occurrences still ahead of now is not spent."""
        series_start = (datetime.now(UTC) - timedelta(days=5)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        automation_id = await self._create_spent_schedule(
            db_context,
            "count-left-schedule",
            recurrence_rule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0;COUNT=10",
            next_scheduled_at=series_start + timedelta(days=2),
            recurrence_anchor=series_start,
        )

        await handle_stale_automation_cleanup(exec_context, {})

        assert (
            await db_context.schedule_automations.get_by_id(
                automation_id, CONVERSATION_ID
            )
            is not None
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
    async def test_evaluates_a_stale_high_frequency_rule_cheaply(
        self, exec_context: ToolExecutionContext, db_context: Database
    ) -> None:
        """A year-old per-second rule must not be walked occurrence by occurrence.

        Reaching the cutoff by iteration is tens of millions of steps computed
        synchronously, which holds the event loop for over a minute and stalls
        every other worker. The budget here is generous against that: the
        walking version took ~77 seconds for this rule.
        """
        automation_id = await self._create_spent_schedule(
            db_context,
            "per-second-schedule",
            recurrence_rule="FREQ=SECONDLY",
            next_scheduled_at=datetime.now(UTC) - timedelta(days=365),
        )

        started = time.perf_counter()
        await handle_stale_automation_cleanup(exec_context, {})
        elapsed = time.perf_counter() - started

        assert elapsed < 5.0, f"cleanup took {elapsed:.1f}s walking the recurrence"
        assert (
            await db_context.schedule_automations.get_by_id(
                automation_id, CONVERSATION_ID
            )
            is not None
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
