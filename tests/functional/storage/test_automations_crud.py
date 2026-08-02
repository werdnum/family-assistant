"""Functional tests for schedule automations repository CRUD operations."""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.database import Database, DatabaseExecutor
from family_assistant.storage.repositories.schedule_automations import (
    ScheduleAutomationsRepository,
)
from family_assistant.storage.tasks import tasks_table
from family_assistant.task_worker import (
    SCHEDULE_AUTOMATION_ADVANCE_OUTBOX_KEY,
    SCHEDULE_AUTOMATION_ADVANCE_TASK_TYPE,
)


@pytest_asyncio.fixture(scope="function")
async def db_context(db_engine: AsyncEngine) -> AsyncGenerator[Database]:
    """
    Provides an entered Database for repository tests.

    Uses the standard db_engine fixture from conftest.py which automatically:
    - Creates a unique database for each test
    - Supports both SQLite and PostgreSQL via --postgres flag
    - Ensures complete test isolation
    """
    db_ctx = Database(engine=db_engine)
    yield db_ctx


class TestScheduleAutomationsRepository:
    """Tests for ScheduleAutomationsRepository CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_schedule_automation(self, db_context: Database) -> None:
        """Test creating a schedule automation."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Daily Summary",
            recurrence_rule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            action_type="wake_llm",
            action_config={"context": "Please send daily summary"},
            conversation_id=conversation_id,
            interface_type="telegram",
            description="Daily morning summary",
            timezone=ZoneInfo("UTC"),
        )

        assert automation_id > 0

        # Verify automation was created
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["name"] == "Daily Summary"
        assert automation["recurrence_rule"] == "FREQ=DAILY;BYHOUR=9;BYMINUTE=0"
        assert automation["action_type"] == "wake_llm"
        assert automation["action_config"].get("context") == "Please send daily summary"
        assert automation["conversation_id"] == conversation_id
        assert automation["description"] == "Daily morning summary"
        assert automation["enabled"] is True
        assert automation["execution_count"] == 0
        assert automation["next_scheduled_at"] is not None

    @pytest.mark.asyncio
    async def test_create_schedule_automation_with_script(
        self, db_context: Database
    ) -> None:
        """Test creating a schedule automation with script action."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Weekly Report",
            recurrence_rule="FREQ=WEEKLY;BYDAY=MO;BYHOUR=10",
            action_type="script",
            action_config={
                "script_code": "print('Weekly report')",
                "task_name": "Weekly Report",
            },
            conversation_id=conversation_id,
            interface_type="telegram",
            timezone=ZoneInfo("UTC"),
        )

        assert automation_id > 0

        # Verify automation was created
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["action_type"] == "script"
        assert (
            automation["action_config"].get("script_code") == "print('Weekly report')"
        )

    @pytest.mark.asyncio
    async def test_create_with_invalid_rrule(self, db_context: Database) -> None:
        """Test creating automation with invalid RRULE raises ValueError."""
        conversation_id = str(uuid.uuid4())

        with pytest.raises(ValueError, match="Invalid RRULE"):
            await db_context.schedule_automations.create(
                name="Bad Rule",
                recurrence_rule="INVALID_RRULE",
                action_type="wake_llm",
                action_config={"context": "test"},
                conversation_id=conversation_id,
                timezone=ZoneInfo("UTC"),
            )

    @pytest.mark.asyncio
    async def test_create_with_invalid_action_type(self, db_context: Database) -> None:
        """Test creating automation with invalid action_type raises ValueError."""
        conversation_id = str(uuid.uuid4())

        with pytest.raises(ValueError, match="Invalid action_type"):
            await db_context.schedule_automations.create(
                name="Bad Action",
                recurrence_rule="FREQ=DAILY;BYHOUR=9",
                action_type="invalid_action",
                action_config={"context": "test"},
                conversation_id=conversation_id,
                timezone=ZoneInfo("UTC"),
            )

    @pytest.mark.asyncio
    async def test_create_duplicate_name_in_conversation(
        self, db_context: Database
    ) -> None:
        """Test creating automation with duplicate name in same conversation fails."""
        conversation_id = str(uuid.uuid4())

        # Create first automation
        await db_context.schedule_automations.create(
            name="Unique Name",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Try to create second with same name
        with pytest.raises(
            ValueError,
            match="A schedule automation named 'Unique Name' already exists",
        ):
            await db_context.schedule_automations.create(
                name="Unique Name",
                recurrence_rule="FREQ=DAILY;BYHOUR=10",
                action_type="wake_llm",
                action_config={"context": "test2"},
                conversation_id=conversation_id,
                timezone=ZoneInfo("UTC"),
            )

    @pytest.mark.asyncio
    async def test_create_same_name_different_conversations(
        self, db_context: Database
    ) -> None:
        """Test creating automation with same name in different conversations succeeds."""
        conv1 = str(uuid.uuid4())
        conv2 = str(uuid.uuid4())

        # Create in first conversation
        id1 = await db_context.schedule_automations.create(
            name="Same Name",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test1"},
            conversation_id=conv1,
            timezone=ZoneInfo("UTC"),
        )

        # Create in second conversation - should succeed
        id2 = await db_context.schedule_automations.create(
            name="Same Name",
            recurrence_rule="FREQ=DAILY;BYHOUR=10",
            action_type="wake_llm",
            action_config={"context": "test2"},
            conversation_id=conv2,
            timezone=ZoneInfo("UTC"),
        )

        assert id1 != id2

    @pytest.mark.asyncio
    async def test_get_by_id(self, db_context: Database) -> None:
        """Test retrieving automation by ID."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Get without conversation filter
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["id"] == automation_id
        assert automation["name"] == "Test Auto"

        # Get with conversation filter
        automation = await db_context.schedule_automations.get_by_id(
            automation_id, conversation_id
        )
        assert automation is not None

        # Get with wrong conversation
        automation = await db_context.schedule_automations.get_by_id(
            automation_id, "wrong_conversation"
        )
        assert automation is None

    @pytest.mark.asyncio
    async def test_get_by_name(self, db_context: Database) -> None:
        """Test retrieving automation by name."""
        conversation_id = str(uuid.uuid4())

        await db_context.schedule_automations.create(
            name="Named Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Get by name
        automation = await db_context.schedule_automations.get_by_name(
            "Named Auto", conversation_id
        )
        assert automation is not None
        assert automation["name"] == "Named Auto"

        # Get with wrong conversation
        automation = await db_context.schedule_automations.get_by_name(
            "Named Auto", "wrong_conversation"
        )
        assert automation is None

        # Get nonexistent name
        automation = await db_context.schedule_automations.get_by_name(
            "Nonexistent", conversation_id
        )
        assert automation is None

    @pytest.mark.asyncio
    async def test_list_all(self, db_context: Database) -> None:
        """Test listing all automations for a conversation."""
        conv1 = str(uuid.uuid4())
        conv2 = str(uuid.uuid4())

        # Create automations in conv1
        await db_context.schedule_automations.create(
            name="Auto 1",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test1"},
            conversation_id=conv1,
            timezone=ZoneInfo("UTC"),
        )
        await db_context.schedule_automations.create(
            name="Auto 2",
            recurrence_rule="FREQ=DAILY;BYHOUR=10",
            action_type="wake_llm",
            action_config={"context": "test2"},
            conversation_id=conv1,
            timezone=ZoneInfo("UTC"),
        )

        # Create automation in conv2
        await db_context.schedule_automations.create(
            name="Auto 3",
            recurrence_rule="FREQ=DAILY;BYHOUR=11",
            action_type="wake_llm",
            action_config={"context": "test3"},
            conversation_id=conv2,
            timezone=ZoneInfo("UTC"),
        )

        # List conv1 automations
        automations = await db_context.schedule_automations.list_all(conv1)
        assert len(automations) == 2
        assert all(a["conversation_id"] == conv1 for a in automations)

        # List conv2 automations
        automations = await db_context.schedule_automations.list_all(conv2)
        assert len(automations) == 1
        assert automations[0]["name"] == "Auto 3"

    @pytest.mark.asyncio
    async def test_list_all_enabled_only(self, db_context: Database) -> None:
        """Test listing only enabled automations."""
        conversation_id = str(uuid.uuid4())

        # Create enabled automation
        await db_context.schedule_automations.create(
            name="Enabled Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test1"},
            conversation_id=conversation_id,
            enabled=True,
            timezone=ZoneInfo("UTC"),
        )

        # Create disabled automation
        id2 = await db_context.schedule_automations.create(
            name="Disabled Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=10",
            action_type="wake_llm",
            action_config={"context": "test2"},
            conversation_id=conversation_id,
            enabled=True,
            timezone=ZoneInfo("UTC"),
        )
        # Disable it
        await db_context.schedule_automations.update_enabled(
            id2,
            conversation_id,
            enabled=False,
            timezone=ZoneInfo("UTC"),
        )

        # List all
        automations = await db_context.schedule_automations.list_all(conversation_id)
        assert len(automations) == 2

        # List enabled only
        automations = await db_context.schedule_automations.list_all(
            conversation_id, enabled_only=True
        )
        assert len(automations) == 1
        assert automations[0]["name"] == "Enabled Auto"

    @pytest.mark.asyncio
    async def test_update_enabled(self, db_context: Database) -> None:
        """Test enabling/disabling automation."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Toggle Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            enabled=True,
            timezone=ZoneInfo("UTC"),
        )

        # Verify initially enabled
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["enabled"] is True

        # Disable
        result = await db_context.schedule_automations.update_enabled(
            automation_id,
            conversation_id,
            enabled=False,
            timezone=ZoneInfo("UTC"),
        )
        assert result is True

        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["enabled"] is False

        # Re-enable
        result = await db_context.schedule_automations.update_enabled(
            automation_id,
            conversation_id,
            enabled=True,
            timezone=ZoneInfo("UTC"),
        )
        assert result is True

        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["enabled"] is True

    @pytest.mark.asyncio
    async def test_update_enabled_wrong_conversation(
        self, db_context: Database
    ) -> None:
        """Test updating enabled status with wrong conversation returns False."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Try to update with wrong conversation
        result = await db_context.schedule_automations.update_enabled(
            automation_id,
            "wrong_conversation",
            enabled=False,
            timezone=ZoneInfo("UTC"),
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_update_name(self, db_context: Database) -> None:
        """Test updating automation name."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Original Name",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Update name
        result = await db_context.schedule_automations.update(
            automation_id,
            conversation_id,
            name="New Name",
            timezone=ZoneInfo("UTC"),
        )
        assert result is True

        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["name"] == "New Name"

    @pytest.mark.asyncio
    async def test_update_description(self, db_context: Database) -> None:
        """Test updating automation description."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            description="Old description",
            timezone=ZoneInfo("UTC"),
        )

        # Update description
        result = await db_context.schedule_automations.update(
            automation_id,
            conversation_id,
            description="New description",
            timezone=ZoneInfo("UTC"),
        )
        assert result is True

        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["description"] == "New description"

        # Clear description (set to None)
        result = await db_context.schedule_automations.update(
            automation_id,
            conversation_id,
            description=None,
            timezone=ZoneInfo("UTC"),
        )
        assert result is True

        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["description"] is None

    @pytest.mark.asyncio
    async def test_update_action_config(self, db_context: Database) -> None:
        """Test updating automation action configuration."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "old context"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Update action config
        new_config = {"context": "new context"}
        result = await db_context.schedule_automations.update(
            automation_id,
            conversation_id,
            action_config=new_config,
            timezone=ZoneInfo("UTC"),
        )
        assert result is True

        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["action_config"].get("context") == "new context"

    @pytest.mark.asyncio
    async def test_update_recurrence_rule(self, db_context: Database) -> None:
        """Test updating recurrence rule recalculates next_scheduled_at."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Get original next_scheduled_at
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None

        # Update recurrence rule
        result = await db_context.schedule_automations.update(
            automation_id,
            conversation_id,
            recurrence_rule="FREQ=DAILY;BYHOUR=15",
            timezone=ZoneInfo("UTC"),
        )
        assert result is True

        # Verify next_scheduled_at was recalculated
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["recurrence_rule"] == "FREQ=DAILY;BYHOUR=15"
        # next_scheduled_at should be different (though we can't easily predict exact time)
        # Just verify it's still set
        assert automation["next_scheduled_at"] is not None

    @pytest.mark.asyncio
    async def test_update_recurrence_rule_invalid(self, db_context: Database) -> None:
        """Test updating with invalid RRULE raises ValueError."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Try to update with invalid RRULE
        with pytest.raises(ValueError, match="Invalid RRULE"):
            await db_context.schedule_automations.update(
                automation_id,
                conversation_id,
                recurrence_rule="INVALID",
                timezone=ZoneInfo("UTC"),
            )

    @pytest.mark.asyncio
    async def test_update_name_collision(self, db_context: Database) -> None:
        """Test updating automation name to an existing name fails."""
        conversation_id = str(uuid.uuid4())

        # Create first automation
        await db_context.schedule_automations.create(
            name="First Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test1"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Create second automation
        auto2_id = await db_context.schedule_automations.create(
            name="Second Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=10",
            action_type="wake_llm",
            action_config={"context": "test2"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Try to rename second automation to first automation's name
        # This should fail due to unique constraint
        with pytest.raises((ValueError, IntegrityError)):
            await db_context.schedule_automations.update(
                auto2_id,
                conversation_id,
                name="First Auto",
                timezone=ZoneInfo("UTC"),
            )

    @pytest.mark.asyncio
    async def test_update_wrong_conversation(self, db_context: Database) -> None:
        """Test updating with wrong conversation returns False."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Try to update with wrong conversation
        result = await db_context.schedule_automations.update(
            automation_id,
            "wrong_conversation",
            name="New Name",
            timezone=ZoneInfo("UTC"),
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_delete(self, db_context: Database) -> None:
        """Test deleting automation."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Verify exists
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None

        # Delete
        result = await db_context.schedule_automations.delete(
            automation_id, conversation_id
        )
        assert result is True

        # Verify deleted
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is None

    @pytest.mark.asyncio
    async def test_delete_failure_leaves_the_queued_task_alive(
        self,
        db_context: Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A delete that fails after cancelling tasks undoes the cancellation.

        Otherwise the automation survives with every queued task cancelled:
        present in the listing, and permanently dead.
        """
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        real_cancel = ScheduleAutomationsRepository._cancel_pending_tasks

        async def cancel_then_fail(
            self: ScheduleAutomationsRepository,
            automation_id: int,
            db: DatabaseExecutor | None = None,
        ) -> int:
            await real_cancel(self, automation_id, db)
            raise RuntimeError("delete interrupted")

        monkeypatch.setattr(
            ScheduleAutomationsRepository, "_cancel_pending_tasks", cancel_then_fail
        )

        with pytest.raises(RuntimeError, match="delete interrupted"):
            await db_context.schedule_automations.delete(automation_id, conversation_id)

        monkeypatch.undo()

        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        rows = await db_context.fetch_all(
            select(tasks_table).where(
                tasks_table.c.payload["automation_id"].as_string() == str(automation_id)
            )
        )
        assert rows, "the automation's task should still be queued"
        assert all(row["status"] == "pending" for row in rows)

    @pytest.mark.asyncio
    async def test_rejected_update_leaves_the_original_task_queued(
        self,
        db_context: Database,
    ) -> None:
        """An edit the database rejects undoes the queue re-sync it did first.

        The name collision is real, not injected: the re-sync (cancel the old
        task, enqueue one built from the new config) runs before the UPDATE
        that the unique constraint refuses. Without a shared transaction the
        automation keeps its old configuration while the queue holds a task
        built from the rejected one.
        """
        conversation_id = str(uuid.uuid4())

        await db_context.schedule_automations.create(
            name="First Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "first"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )
        automation_id = await db_context.schedule_automations.create(
            name="Second Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=10",
            action_type="wake_llm",
            action_config={"context": "original"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )
        original_tasks = await self._automation_tasks(db_context, automation_id)
        assert original_tasks, "creating the automation should queue a task"

        # Changing action_config forces the queue re-sync; the colliding name
        # is what makes the UPDATE that follows it fail.
        with pytest.raises((ValueError, IntegrityError)):
            await db_context.schedule_automations.update(
                automation_id,
                conversation_id,
                name="First Auto",
                action_config={"context": "rewritten"},
                timezone=ZoneInfo("UTC"),
            )

        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["name"] == "Second Auto"
        assert automation["action_config"].get("context") == "original"
        assert await self._automation_tasks(db_context, automation_id) == original_tasks

    @staticmethod
    async def _automation_tasks(
        db_context: Database, automation_id: int
    ) -> list[tuple[str, str]]:
        """The (task_id, status) pairs queued for an automation."""
        rows = await db_context.fetch_all(
            select(tasks_table)
            .where(
                tasks_table.c.payload["automation_id"].as_string() == str(automation_id)
            )
            .order_by(tasks_table.c.task_id)
        )
        return [(row["task_id"], row["status"]) for row in rows]

    @pytest.mark.asyncio
    async def test_delete_wrong_conversation(self, db_context: Database) -> None:
        """Test deleting with wrong conversation returns False."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Try to delete with wrong conversation
        result = await db_context.schedule_automations.delete(
            automation_id, "wrong_conversation"
        )
        assert result is False

        # Verify still exists
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None

    @pytest.mark.asyncio
    async def test_get_execution_stats(self, db_context: Database) -> None:
        """Test getting execution statistics."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Get stats for new automation
        stats = await db_context.schedule_automations.get_execution_stats(automation_id)
        assert stats is not None
        assert stats["total_executions"] == 0
        assert stats["last_execution_at"] is None
        assert stats["next_scheduled_at"] is not None
        assert stats["recent_executions"] == []

    @pytest.mark.asyncio
    async def test_after_task_execution_updates_stats(
        self, db_context: Database
    ) -> None:
        """Test after_task_execution updates execution count and timestamp."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Get initial state
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["execution_count"] == 0
        assert automation["last_execution_at"] is None

        # Simulate task execution
        execution_time = datetime.now(UTC)
        await db_context.schedule_automations.after_task_execution(
            automation_id,
            execution_time,
            timezone=ZoneInfo("UTC"),
        )

        # Verify stats were updated
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["execution_count"] == 1
        assert automation["last_execution_at"] is not None
        # Check that last_execution_at is close to execution_time (within 1 second)
        last_exec = automation["last_execution_at"]
        time_diff = abs((last_exec - execution_time).total_seconds())
        assert time_diff < 1

    @pytest.mark.asyncio
    async def test_after_task_execution_schedules_next(
        self, db_context: Database
    ) -> None:
        """Test after_task_execution schedules next task instance."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Get initial next_scheduled_at
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None

        # Simulate task execution
        execution_time = datetime.now(UTC)
        await db_context.schedule_automations.after_task_execution(
            automation_id,
            execution_time,
            timezone=ZoneInfo("UTC"),
        )

        # Verify next_scheduled_at was updated
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        new_next = automation["next_scheduled_at"]
        assert new_next is not None  # Should be set after execution
        # Next scheduled time should be after the execution time
        assert new_next > execution_time

    @pytest.mark.asyncio
    async def test_after_task_execution_disabled_automation(
        self, db_context: Database
    ) -> None:
        """Test after_task_execution updates stats but doesn't schedule next task for disabled automation."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            enabled=True,
            timezone=ZoneInfo("UTC"),
        )

        # Disable the automation
        await db_context.schedule_automations.update_enabled(
            automation_id,
            conversation_id,
            enabled=False,
            timezone=ZoneInfo("UTC"),
        )

        # Get initial execution count
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["execution_count"] == 0

        # Simulate task execution
        execution_time = datetime.now(UTC)
        await db_context.schedule_automations.after_task_execution(
            automation_id,
            execution_time,
            timezone=ZoneInfo("UTC"),
        )

        # Verify stats WERE updated (execution happened so it should be recorded)
        # but next task wasn't scheduled (automation is disabled)
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["execution_count"] == 1  # Should be incremented
        assert automation["last_execution_at"] is not None  # Should be set

    @pytest.mark.asyncio
    async def test_create_with_timezone_interprets_rrule_in_local_time(
        self, db_context: Database
    ) -> None:
        """Test that RRULE BYHOUR is interpreted in the given timezone, not UTC."""
        conversation_id = str(uuid.uuid4())
        sydney_tz = ZoneInfo("Australia/Sydney")

        automation_id = await db_context.schedule_automations.create(
            name="Sydney Morning",
            recurrence_rule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            action_type="wake_llm",
            action_config={"context": "Good morning Sydney"},
            conversation_id=conversation_id,
            timezone=sydney_tz,
        )

        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        next_at = automation["next_scheduled_at"]
        assert next_at is not None

        # The stored time is UTC. Convert to Sydney to verify it's 9:00 local.
        next_sydney = next_at.astimezone(sydney_tz)
        assert next_sydney.hour == 9
        assert next_sydney.minute == 0

    @pytest.mark.asyncio
    async def test_timezone_differs_from_utc(self, db_context: Database) -> None:
        """Test that timezone-aware scheduling differs from UTC-based scheduling."""
        conversation_id = str(uuid.uuid4())
        sydney_tz = ZoneInfo("Australia/Sydney")

        # Create with Sydney timezone
        auto_sydney = await db_context.schedule_automations.create(
            name="Sydney Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=sydney_tz,
        )

        # Create with UTC (no timezone)
        auto_utc = await db_context.schedule_automations.create(
            name="UTC Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        sydney_auto = await db_context.schedule_automations.get_by_id(auto_sydney)
        utc_auto = await db_context.schedule_automations.get_by_id(auto_utc)
        assert sydney_auto is not None
        assert utc_auto is not None

        sydney_next = sydney_auto["next_scheduled_at"]
        utc_next = utc_auto["next_scheduled_at"]
        assert sydney_next is not None
        assert utc_next is not None

        # 9am Sydney and 9am UTC should schedule at different UTC instants.
        assert sydney_next != utc_next

        # Verify each is 9:00 in its respective timezone
        assert sydney_next.astimezone(sydney_tz).hour == 9
        assert utc_next.astimezone(ZoneInfo("UTC")).hour == 9

    @pytest.mark.asyncio
    async def test_after_task_execution_with_timezone(
        self, db_context: Database
    ) -> None:
        """Test that after_task_execution schedules next occurrence in the correct timezone."""
        conversation_id = str(uuid.uuid4())
        sydney_tz = ZoneInfo("Australia/Sydney")

        automation_id = await db_context.schedule_automations.create(
            name="Sydney Recurring",
            recurrence_rule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=sydney_tz,
        )

        # Simulate execution at 2026-02-28 22:05 UTC = March 1 09:05 AEDT
        execution_time = datetime(2026, 2, 28, 22, 5, 0, tzinfo=UTC)
        await db_context.schedule_automations.after_task_execution(
            automation_id, execution_time, timezone=sydney_tz
        )

        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        next_at = automation["next_scheduled_at"]
        assert next_at is not None

        # Next 9am Sydney after March 1 09:05 AEDT is March 2 09:00 AEDT
        next_sydney = next_at.astimezone(sydney_tz)
        assert next_sydney.hour == 9
        assert next_sydney.minute == 0
        assert next_sydney.day == 2

    @pytest.mark.asyncio
    async def test_parse_rrule_naive_after_treated_as_utc(
        self, db_context: Database
    ) -> None:
        """Naive ``after`` datetime should be assumed UTC before timezone conversion."""
        conversation_id = str(uuid.uuid4())
        sydney_tz = ZoneInfo("Australia/Sydney")

        automation_id = await db_context.schedule_automations.create(
            name="Naive After",
            recurrence_rule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=sydney_tz,
        )

        # Simulate execution with a naive datetime (no tzinfo).
        # The code should treat it as UTC, not system-local time.
        naive_execution = datetime(2026, 2, 28, 22, 5, 0)
        await db_context.schedule_automations.after_task_execution(
            automation_id, naive_execution, timezone=sydney_tz
        )

        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        next_at = automation["next_scheduled_at"]
        assert next_at is not None

        # Naive 22:05 UTC = March 1 09:05 AEDT, so next 9am Sydney is March 2
        next_sydney = next_at.astimezone(sydney_tz)
        assert next_sydney.hour == 9
        assert next_sydney.minute == 0

    @pytest.mark.asyncio
    async def test_re_enable_recalculates_next_scheduled_at(
        self, db_context: Database
    ) -> None:
        """Re-enabling an automation should recalculate next_scheduled_at from now."""
        conversation_id = str(uuid.uuid4())
        sydney_tz = ZoneInfo("Australia/Sydney")

        automation_id = await db_context.schedule_automations.create(
            name="Re-enable Test",
            recurrence_rule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=sydney_tz,
        )

        # Record original next_scheduled_at
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        original_next = automation["next_scheduled_at"]
        assert original_next is not None

        # Disable
        result = await db_context.schedule_automations.update_enabled(
            automation_id,
            conversation_id,
            enabled=False,
            timezone=ZoneInfo("UTC"),
        )
        assert result is True

        # Re-enable with timezone
        result = await db_context.schedule_automations.update_enabled(
            automation_id, conversation_id, enabled=True, timezone=sydney_tz
        )
        assert result is True

        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["enabled"] is True
        new_next = automation["next_scheduled_at"]
        assert new_next is not None

        # The recalculated time should still be 9am Sydney
        next_sydney = new_next.astimezone(sydney_tz)
        assert next_sydney.hour == 9
        assert next_sydney.minute == 0


async def _get_pending_tasks_for_automation(
    db_context: Database, automation_id: int
) -> list[dict]:
    """Helper to query pending tasks for a specific automation."""
    stmt = select(tasks_table).where(
        tasks_table.c.status == "pending",
        tasks_table.c.task_id.like(f"sched_auto_{automation_id}_%"),
    )
    rows = await db_context.fetch_all(stmt)
    return [dict(row) for row in rows]


async def _get_all_tasks_for_automation(
    db_context: Database, automation_id: int
) -> list[dict]:
    """Helper to query all tasks (any status) for a specific automation."""
    stmt = select(tasks_table).where(
        tasks_table.c.task_id.like(f"sched_auto_{automation_id}_%"),
    )
    rows = await db_context.fetch_all(stmt)
    return [dict(row) for row in rows]


async def _get_task_by_id(db_context: Database, task_id: str) -> dict | None:
    """Helper to query a task by ID."""
    stmt = select(tasks_table).where(tasks_table.c.task_id == task_id)
    row = await db_context.fetch_one(stmt)
    return dict(row) if row is not None else None


class TestTaskQueueSync:
    """Tests for task queue synchronization when automations are modified."""

    @pytest.mark.asyncio
    async def test_disable_cancels_pending_tasks(self, db_context: Database) -> None:
        """Disabling an automation cancels its pending task queue items."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Daily Task",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "daily check"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Verify a pending task was created
        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 1

        # Disable the automation
        await db_context.schedule_automations.update_enabled(
            automation_id,
            conversation_id,
            enabled=False,
            timezone=ZoneInfo("UTC"),
        )

        # Verify pending task was cancelled
        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 0

        all_tasks = await _get_all_tasks_for_automation(db_context, automation_id)
        assert len(all_tasks) == 1
        assert all_tasks[0]["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_disable_preserves_pending_schedule_advance_task(
        self, db_context: Database
    ) -> None:
        """Disabling cancels future runs without losing terminal stats advancement."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Daily Task",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "daily check"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 1
        scheduled_task_id = pending[0]["task_id"]
        advance_task_id = f"sched_auto_advance_{automation_id}_test"
        source_outbox_task_id = f"sched_auto_source_{automation_id}_test"
        source_outbox_payload = {
            "automation_id": str(automation_id),
            "automation_type": "schedule",
            SCHEDULE_AUTOMATION_ADVANCE_OUTBOX_KEY: {
                "automation_id": str(automation_id),
                "source_task_id": source_outbox_task_id,
                "execution_time": datetime(2026, 6, 22, 4, 29, tzinfo=UTC).isoformat(),
                "schedule_next": True,
            },
        }
        await db_context.tasks.enqueue(
            task_id=advance_task_id,
            task_type=SCHEDULE_AUTOMATION_ADVANCE_TASK_TYPE,
            payload={
                "automation_id": str(automation_id),
                "source_task_id": scheduled_task_id,
                "execution_time": datetime(2026, 6, 22, 4, 28, tzinfo=UTC).isoformat(),
            },
        )
        await db_context.tasks.enqueue(
            task_id=source_outbox_task_id,
            task_type="script_execution",
            payload=source_outbox_payload,
        )
        await db_context.tasks.update_status(
            source_outbox_task_id,
            "done",
            payload=source_outbox_payload,
        )

        await db_context.schedule_automations.update_enabled(
            automation_id,
            conversation_id,
            enabled=False,
            timezone=ZoneInfo("UTC"),
        )

        scheduled_task = await _get_task_by_id(db_context, scheduled_task_id)
        advance_task = await _get_task_by_id(db_context, advance_task_id)
        source_outbox_task = await _get_task_by_id(db_context, source_outbox_task_id)
        assert scheduled_task is not None
        assert advance_task is not None
        assert source_outbox_task is not None
        assert scheduled_task["status"] == "cancelled"
        assert advance_task["status"] == "pending"
        advance_payload = advance_task["payload"]
        assert advance_payload is not None
        assert advance_payload["schedule_next"] is False
        source_payload = source_outbox_task["payload"]
        assert source_payload is not None
        source_outbox = source_payload[SCHEDULE_AUTOMATION_ADVANCE_OUTBOX_KEY]
        assert source_outbox["schedule_next"] is False

        await db_context.schedule_automations.update_enabled(
            automation_id,
            conversation_id,
            enabled=True,
            timezone=ZoneInfo("UTC"),
        )
        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 1

        await db_context.schedule_automations.after_task_execution(
            automation_id,
            datetime.fromisoformat(advance_payload["execution_time"]),
            timezone=ZoneInfo("UTC"),
            schedule_next=advance_payload["schedule_next"],
        )

        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 1
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["execution_count"] == 1

        await db_context.schedule_automations.after_task_execution(
            automation_id,
            datetime.fromisoformat(source_outbox["execution_time"]),
            timezone=ZoneInfo("UTC"),
            schedule_next=source_outbox["schedule_next"],
        )

        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 1
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["execution_count"] == 2

    @pytest.mark.asyncio
    async def test_stats_only_advance_does_not_rewind_last_execution_at(
        self, db_context: Database
    ) -> None:
        """A delayed stats-only advance should not move last_execution_at backward."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Hourly Task",
            recurrence_rule="FREQ=HOURLY",
            action_type="wake_llm",
            action_config={"context": "hourly check"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        newer_execution_time = datetime(2026, 6, 23, 5, 0, tzinfo=UTC)
        older_execution_time = datetime(2026, 6, 23, 4, 0)
        await db_context.schedule_automations.after_task_execution(
            automation_id,
            newer_execution_time,
            timezone=ZoneInfo("UTC"),
            schedule_next=False,
        )
        await db_context.schedule_automations.after_task_execution(
            automation_id,
            older_execution_time,
            timezone=ZoneInfo("UTC"),
            schedule_next=False,
        )

        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["last_execution_at"] == newer_execution_time
        assert automation["execution_count"] == 2

    @pytest.mark.asyncio
    async def test_enable_schedules_new_task(self, db_context: Database) -> None:
        """Re-enabling an automation schedules a new task."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Daily Task",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "daily check"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Disable (cancels pending tasks)
        await db_context.schedule_automations.update_enabled(
            automation_id,
            conversation_id,
            enabled=False,
            timezone=ZoneInfo("UTC"),
        )
        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 0

        # Re-enable (should schedule a new task)
        await db_context.schedule_automations.update_enabled(
            automation_id,
            conversation_id,
            enabled=True,
            timezone=ZoneInfo("UTC"),
        )
        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 1

    @pytest.mark.asyncio
    async def test_update_action_config_reschedules_task(
        self, db_context: Database
    ) -> None:
        """Updating action_config cancels old task and creates new one with updated payload."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Daily Task",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "old context"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Verify initial task has old context
        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 1
        assert pending[0]["payload"]["callback_context"] == "old context"

        # Update action_config
        await db_context.schedule_automations.update(
            automation_id,
            conversation_id,
            action_config={"context": "new context"},
            timezone=ZoneInfo("UTC"),
        )

        # Verify old task was cancelled and new one created with new context
        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 1
        assert pending[0]["payload"]["callback_context"] == "new context"

    @pytest.mark.asyncio
    async def test_update_action_config_script_reschedules_task(
        self, db_context: Database
    ) -> None:
        """Updating script action_config creates new task with updated script code."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Script Task",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="script",
            action_config={"script_code": "print('old')", "task_name": "Old Script"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Verify initial task
        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 1
        assert pending[0]["payload"]["script_code"] == "print('old')"

        # Update action_config
        await db_context.schedule_automations.update(
            automation_id,
            conversation_id,
            action_config={"script_code": "print('new')", "task_name": "New Script"},
            timezone=ZoneInfo("UTC"),
        )

        # Verify new task has updated payload
        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 1
        assert pending[0]["payload"]["script_code"] == "print('new')"
        assert pending[0]["payload"]["task_name"] == "New Script"

    @pytest.mark.asyncio
    async def test_update_enabled_false_via_update_cancels_tasks(
        self, db_context: Database
    ) -> None:
        """Setting enabled=False via update() cancels pending tasks."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Daily Task",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Verify pending task exists
        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 1

        # Disable via update method
        await db_context.schedule_automations.update(
            automation_id,
            conversation_id,
            enabled=False,
            timezone=ZoneInfo("UTC"),
        )

        # Verify pending task was cancelled
        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_update_enabled_true_via_update_schedules_task(
        self, db_context: Database
    ) -> None:
        """Setting enabled=True via update() on a disabled automation schedules a new task."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Daily Task",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Disable first
        await db_context.schedule_automations.update_enabled(
            automation_id,
            conversation_id,
            enabled=False,
            timezone=ZoneInfo("UTC"),
        )
        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 0

        # Re-enable via update method
        await db_context.schedule_automations.update(
            automation_id,
            conversation_id,
            enabled=True,
            timezone=ZoneInfo("UTC"),
        )

        # Verify new task was scheduled
        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 1

    @pytest.mark.asyncio
    async def test_delete_cancels_pending_tasks(self, db_context: Database) -> None:
        """Deleting an automation cancels its pending task queue items."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Daily Task",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Verify pending task exists
        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 1

        # Delete the automation
        await db_context.schedule_automations.delete(automation_id, conversation_id)

        # Verify pending task was cancelled
        all_tasks = await _get_all_tasks_for_automation(db_context, automation_id)
        assert all(t["status"] == "cancelled" for t in all_tasks)

    @pytest.mark.asyncio
    async def test_update_recurrence_rule_reschedules_task(
        self, db_context: Database
    ) -> None:
        """Updating recurrence_rule cancels old task and schedules new one."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Daily Task",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Get initial pending task
        pending_before = await _get_pending_tasks_for_automation(
            db_context, automation_id
        )
        assert len(pending_before) == 1
        old_task_id = pending_before[0]["task_id"]

        # Update recurrence rule
        await db_context.schedule_automations.update(
            automation_id,
            conversation_id,
            recurrence_rule="FREQ=DAILY;BYHOUR=15",
            timezone=ZoneInfo("UTC"),
        )

        # Verify new pending task exists with a different task_id
        pending_after = await _get_pending_tasks_for_automation(
            db_context, automation_id
        )
        assert len(pending_after) == 1
        assert pending_after[0]["task_id"] != old_task_id

    @pytest.mark.asyncio
    async def test_enable_already_enabled_reschedules(
        self, db_context: Database
    ) -> None:
        """Calling update_enabled(True) on an already-enabled automation reschedules."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Daily Task",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Get initial task ID
        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 1
        original_task_id = pending[0]["task_id"]

        # Update enabled to True (same as current) — reschedules to recalculate next_at
        await db_context.schedule_automations.update_enabled(
            automation_id,
            conversation_id,
            enabled=True,
            timezone=ZoneInfo("UTC"),
        )

        # Verify a new task was scheduled (old one cancelled, new one created)
        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 1
        assert pending[0]["task_id"] != original_task_id

    @pytest.mark.asyncio
    async def test_update_description_no_task_sync(self, db_context: Database) -> None:
        """Updating non-task-affecting fields doesn't cancel/reschedule tasks."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Daily Task",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Get initial task ID
        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 1
        original_task_id = pending[0]["task_id"]

        # Update description only
        await db_context.schedule_automations.update(
            automation_id,
            conversation_id,
            description="Updated description",
            timezone=ZoneInfo("UTC"),
        )

        # Verify same task still exists
        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 1
        assert pending[0]["task_id"] == original_task_id

    @pytest.mark.asyncio
    async def test_enable_updates_next_scheduled_at_in_db(
        self, db_context: Database
    ) -> None:
        """Re-enabling via update_enabled persists next_scheduled_at to the automation record."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Daily Task",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Disable
        await db_context.schedule_automations.update_enabled(
            automation_id,
            conversation_id,
            enabled=False,
            timezone=ZoneInfo("UTC"),
        )

        # Re-enable
        await db_context.schedule_automations.update_enabled(
            automation_id,
            conversation_id,
            enabled=True,
            timezone=ZoneInfo("UTC"),
        )

        # Verify next_scheduled_at matches the newly scheduled task
        automation = await db_context.schedule_automations.get_by_id(
            automation_id, conversation_id
        )
        assert automation is not None

        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 1
        # Compare without tzinfo since SQLite returns naive datetimes
        assert automation["next_scheduled_at"] is not None
        auto_dt = automation["next_scheduled_at"].replace(tzinfo=None)
        task_dt = pending[0]["scheduled_at"]
        if task_dt.tzinfo is not None:
            task_dt = task_dt.replace(tzinfo=None)
        assert auto_dt == task_dt

    @pytest.mark.asyncio
    async def test_update_name_reschedules_script_task(
        self, db_context: Database
    ) -> None:
        """Updating name on a script automation (without explicit task_name) reschedules task."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Old Script Name",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="script",
            action_config={"script_code": "print('hello')"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Verify initial task uses automation name as task_name
        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 1
        assert pending[0]["payload"]["task_name"] == "Old Script Name"
        old_task_id = pending[0]["task_id"]

        # Update name only
        await db_context.schedule_automations.update(
            automation_id,
            conversation_id,
            name="New Script Name",
            timezone=ZoneInfo("UTC"),
        )

        # Verify task was rescheduled with new name
        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 1
        assert pending[0]["task_id"] != old_task_id
        assert pending[0]["payload"]["task_name"] == "New Script Name"

    @pytest.mark.asyncio
    async def test_update_name_no_resched_when_task_name_in_action_config(
        self, db_context: Database
    ) -> None:
        """Updating name on a script automation with explicit task_name doesn't reschedule."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Automation Name",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="script",
            action_config={
                "script_code": "print('hello')",
                "task_name": "Explicit Name",
            },
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 1
        original_task_id = pending[0]["task_id"]

        # Update name only - should NOT trigger resched since task_name is in action_config
        await db_context.schedule_automations.update(
            automation_id,
            conversation_id,
            name="New Automation Name",
            timezone=ZoneInfo("UTC"),
        )

        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 1
        assert pending[0]["task_id"] == original_task_id

    @pytest.mark.asyncio
    async def test_update_name_no_resched_for_wake_llm(
        self, db_context: Database
    ) -> None:
        """Updating name on a wake_llm automation doesn't reschedule (name not in payload)."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Daily Summary",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 1
        original_task_id = pending[0]["task_id"]

        # Update name only - should NOT trigger resched for wake_llm
        await db_context.schedule_automations.update(
            automation_id,
            conversation_id,
            name="Updated Summary",
            timezone=ZoneInfo("UTC"),
        )

        pending = await _get_pending_tasks_for_automation(db_context, automation_id)
        assert len(pending) == 1
        assert pending[0]["task_id"] == original_task_id
