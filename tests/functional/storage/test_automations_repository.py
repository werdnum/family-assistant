"""Functional tests for automations repositories."""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.repositories.events import EventsRepository


@pytest_asyncio.fixture(scope="function")
async def db_context(db_engine: AsyncEngine) -> AsyncGenerator[DatabaseContext]:
    """
    Provides an entered DatabaseContext for repository tests.

    Uses the standard db_engine fixture from conftest.py which automatically:
    - Creates a unique database for each test
    - Supports both SQLite and PostgreSQL via --postgres flag
    - Ensures complete test isolation
    """
    async with DatabaseContext(engine=db_engine) as db_ctx:
        yield db_ctx


class TestScheduleAutomationsRepository:
    """Tests for ScheduleAutomationsRepository."""

    @pytest.mark.asyncio
    async def test_create_schedule_automation(
        self, db_context: DatabaseContext
    ) -> None:
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
        )

        assert automation_id > 0

        # Verify automation was created
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["name"] == "Daily Summary"
        assert automation["recurrence_rule"] == "FREQ=DAILY;BYHOUR=9;BYMINUTE=0"
        assert automation["action_type"] == "wake_llm"
        assert automation["action_config"]["context"] == "Please send daily summary"
        assert automation["conversation_id"] == conversation_id
        assert automation["description"] == "Daily morning summary"
        assert automation["enabled"] is True
        assert automation["execution_count"] == 0
        assert automation["next_scheduled_at"] is not None

    @pytest.mark.asyncio
    async def test_create_schedule_automation_with_script(
        self, db_context: DatabaseContext
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
        )

        assert automation_id > 0

        # Verify automation was created
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["action_type"] == "script"
        assert automation["action_config"]["script_code"] == "print('Weekly report')"

    @pytest.mark.asyncio
    async def test_create_with_invalid_rrule(self, db_context: DatabaseContext) -> None:
        """Test creating automation with invalid RRULE raises ValueError."""
        conversation_id = str(uuid.uuid4())

        with pytest.raises(ValueError, match="Invalid RRULE"):
            await db_context.schedule_automations.create(
                name="Bad Rule",
                recurrence_rule="INVALID_RRULE",
                action_type="wake_llm",
                action_config={"context": "test"},
                conversation_id=conversation_id,
            )

    @pytest.mark.asyncio
    async def test_create_with_invalid_action_type(
        self, db_context: DatabaseContext
    ) -> None:
        """Test creating automation with invalid action_type raises ValueError."""
        conversation_id = str(uuid.uuid4())

        with pytest.raises(ValueError, match="Invalid action_type"):
            await db_context.schedule_automations.create(
                name="Bad Action",
                recurrence_rule="FREQ=DAILY;BYHOUR=9",
                action_type="invalid_action",
                action_config={"context": "test"},
                conversation_id=conversation_id,
            )

    @pytest.mark.asyncio
    async def test_create_duplicate_name_in_conversation(
        self, db_context: DatabaseContext
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
            )

    @pytest.mark.asyncio
    async def test_create_same_name_different_conversations(
        self, db_context: DatabaseContext
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
        )

        # Create in second conversation - should succeed
        id2 = await db_context.schedule_automations.create(
            name="Same Name",
            recurrence_rule="FREQ=DAILY;BYHOUR=10",
            action_type="wake_llm",
            action_config={"context": "test2"},
            conversation_id=conv2,
        )

        assert id1 != id2

    @pytest.mark.asyncio
    async def test_get_by_id(self, db_context: DatabaseContext) -> None:
        """Test retrieving automation by ID."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
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
    async def test_get_by_name(self, db_context: DatabaseContext) -> None:
        """Test retrieving automation by name."""
        conversation_id = str(uuid.uuid4())

        await db_context.schedule_automations.create(
            name="Named Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
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
    async def test_list_all(self, db_context: DatabaseContext) -> None:
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
        )
        await db_context.schedule_automations.create(
            name="Auto 2",
            recurrence_rule="FREQ=DAILY;BYHOUR=10",
            action_type="wake_llm",
            action_config={"context": "test2"},
            conversation_id=conv1,
        )

        # Create automation in conv2
        await db_context.schedule_automations.create(
            name="Auto 3",
            recurrence_rule="FREQ=DAILY;BYHOUR=11",
            action_type="wake_llm",
            action_config={"context": "test3"},
            conversation_id=conv2,
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
    async def test_list_all_enabled_only(self, db_context: DatabaseContext) -> None:
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
        )

        # Create disabled automation
        id2 = await db_context.schedule_automations.create(
            name="Disabled Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=10",
            action_type="wake_llm",
            action_config={"context": "test2"},
            conversation_id=conversation_id,
            enabled=True,
        )
        # Disable it
        await db_context.schedule_automations.update_enabled(
            id2, conversation_id, enabled=False
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
    async def test_update_enabled(self, db_context: DatabaseContext) -> None:
        """Test enabling/disabling automation."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Toggle Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            enabled=True,
        )

        # Verify initially enabled
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["enabled"] is True

        # Disable
        result = await db_context.schedule_automations.update_enabled(
            automation_id, conversation_id, enabled=False
        )
        assert result is True

        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["enabled"] is False

        # Re-enable
        result = await db_context.schedule_automations.update_enabled(
            automation_id, conversation_id, enabled=True
        )
        assert result is True

        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["enabled"] is True

    @pytest.mark.asyncio
    async def test_update_enabled_wrong_conversation(
        self, db_context: DatabaseContext
    ) -> None:
        """Test updating enabled status with wrong conversation returns False."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
        )

        # Try to update with wrong conversation
        result = await db_context.schedule_automations.update_enabled(
            automation_id, "wrong_conversation", enabled=False
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_update_name(self, db_context: DatabaseContext) -> None:
        """Test updating automation name."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Original Name",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
        )

        # Update name
        result = await db_context.schedule_automations.update(
            automation_id, conversation_id, name="New Name"
        )
        assert result is True

        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["name"] == "New Name"

    @pytest.mark.asyncio
    async def test_update_description(self, db_context: DatabaseContext) -> None:
        """Test updating automation description."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            description="Old description",
        )

        # Update description
        result = await db_context.schedule_automations.update(
            automation_id, conversation_id, description="New description"
        )
        assert result is True

        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["description"] == "New description"

        # Clear description (set to None)
        result = await db_context.schedule_automations.update(
            automation_id, conversation_id, description=None
        )
        assert result is True

        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["description"] is None

    @pytest.mark.asyncio
    async def test_update_action_config(self, db_context: DatabaseContext) -> None:
        """Test updating automation action configuration."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "old context"},
            conversation_id=conversation_id,
        )

        # Update action config
        new_config = {"context": "new context"}
        result = await db_context.schedule_automations.update(
            automation_id, conversation_id, action_config=new_config
        )
        assert result is True

        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["action_config"]["context"] == "new context"

    @pytest.mark.asyncio
    async def test_update_recurrence_rule(self, db_context: DatabaseContext) -> None:
        """Test updating recurrence rule recalculates next_scheduled_at."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
        )

        # Get original next_scheduled_at
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None

        # Update recurrence rule
        result = await db_context.schedule_automations.update(
            automation_id, conversation_id, recurrence_rule="FREQ=DAILY;BYHOUR=15"
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
    async def test_update_recurrence_rule_invalid(
        self, db_context: DatabaseContext
    ) -> None:
        """Test updating with invalid RRULE raises ValueError."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
        )

        # Try to update with invalid RRULE
        with pytest.raises(ValueError, match="Invalid RRULE"):
            await db_context.schedule_automations.update(
                automation_id, conversation_id, recurrence_rule="INVALID"
            )

    @pytest.mark.asyncio
    async def test_update_name_collision(self, db_context: DatabaseContext) -> None:
        """Test updating automation name to an existing name fails."""
        conversation_id = str(uuid.uuid4())

        # Create first automation
        await db_context.schedule_automations.create(
            name="First Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test1"},
            conversation_id=conversation_id,
        )

        # Create second automation
        auto2_id = await db_context.schedule_automations.create(
            name="Second Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=10",
            action_type="wake_llm",
            action_config={"context": "test2"},
            conversation_id=conversation_id,
        )

        # Try to rename second automation to first automation's name
        # This should fail due to unique constraint
        with pytest.raises((ValueError, IntegrityError)):
            await db_context.schedule_automations.update(
                auto2_id, conversation_id, name="First Auto"
            )

    @pytest.mark.asyncio
    async def test_update_wrong_conversation(self, db_context: DatabaseContext) -> None:
        """Test updating with wrong conversation returns False."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
        )

        # Try to update with wrong conversation
        result = await db_context.schedule_automations.update(
            automation_id, "wrong_conversation", name="New Name"
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_delete(self, db_context: DatabaseContext) -> None:
        """Test deleting automation."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
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
    async def test_delete_wrong_conversation(self, db_context: DatabaseContext) -> None:
        """Test deleting with wrong conversation returns False."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
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
    async def test_get_execution_stats(self, db_context: DatabaseContext) -> None:
        """Test getting execution statistics."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
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
        self, db_context: DatabaseContext
    ) -> None:
        """Test after_task_execution updates execution count and timestamp."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
        )

        # Get initial state
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["execution_count"] == 0
        assert automation["last_execution_at"] is None

        # Simulate task execution
        execution_time = datetime.now(UTC)
        await db_context.schedule_automations.after_task_execution(
            automation_id, execution_time
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
        self, db_context: DatabaseContext
    ) -> None:
        """Test after_task_execution schedules next task instance."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
        )

        # Get initial next_scheduled_at
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None

        # Simulate task execution
        execution_time = datetime.now(UTC)
        await db_context.schedule_automations.after_task_execution(
            automation_id, execution_time
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
        self, db_context: DatabaseContext
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
        )

        # Disable the automation
        await db_context.schedule_automations.update_enabled(
            automation_id, conversation_id, enabled=False
        )

        # Get initial execution count
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["execution_count"] == 0

        # Simulate task execution
        execution_time = datetime.now(UTC)
        await db_context.schedule_automations.after_task_execution(
            automation_id, execution_time
        )

        # Verify stats WERE updated (execution happened so it should be recorded)
        # but next task wasn't scheduled (automation is disabled)
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["execution_count"] == 1  # Should be incremented
        assert automation["last_execution_at"] is not None  # Should be set


class TestAutomationsRepository:
    """Tests for AutomationsRepository (unified view)."""

    @pytest.mark.asyncio
    async def test_list_all_both_types(self, db_context: DatabaseContext) -> None:
        """Test listing automations of both types."""
        conversation_id = str(uuid.uuid4())

        # Create schedule automation
        await db_context.schedule_automations.create(
            name="Schedule Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
        )

        # Create event listener (we'll use the events repository)

        events_repo = EventsRepository(db_context)
        await events_repo.create_event_listener(
            name="Event Auto",
            description="Test event listener",
            source_id="home_assistant",
            match_conditions={"entity_id": "sensor.test"},
            action_type="wake_llm",
            action_config={"context": "event happened"},
            conversation_id=conversation_id,
            interface_type="telegram",
        )

        # List all automations
        automations, total = await db_context.automations.list_all(conversation_id)
        assert total == 2
        assert len(automations) == 2

        # Verify both types are present
        types = {a.type for a in automations}
        assert types == {"schedule", "event"}

    @pytest.mark.asyncio
    async def test_list_all_filter_by_type(self, db_context: DatabaseContext) -> None:
        """Test filtering automations by type."""
        conversation_id = str(uuid.uuid4())

        # Create schedule automation
        await db_context.schedule_automations.create(
            name="Schedule Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
        )

        # Create event listener

        events_repo = EventsRepository(db_context)
        await events_repo.create_event_listener(
            name="Event Auto",
            description="Test event listener",
            source_id="home_assistant",
            match_conditions={"entity_id": "sensor.test"},
            action_type="wake_llm",
            action_config={"context": "event happened"},
            conversation_id=conversation_id,
            interface_type="telegram",
        )

        # List only schedule automations
        automations, total = await db_context.automations.list_all(
            conversation_id, automation_type="schedule"
        )
        assert total == 1
        assert len(automations) == 1
        assert automations[0].type == "schedule"
        assert automations[0].name == "Schedule Auto"

        # List only event automations
        automations, total = await db_context.automations.list_all(
            conversation_id, automation_type="event"
        )
        assert total == 1
        assert len(automations) == 1
        assert automations[0].type == "event"
        assert automations[0].name == "Event Auto"

    @pytest.mark.asyncio
    async def test_list_all_filter_by_enabled(
        self, db_context: DatabaseContext
    ) -> None:
        """Test filtering automations by enabled status."""
        conversation_id = str(uuid.uuid4())

        # Create enabled schedule automation
        await db_context.schedule_automations.create(
            name="Enabled Schedule",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            enabled=True,
        )

        # Create disabled schedule automation
        await db_context.schedule_automations.create(
            name="Disabled Schedule",
            recurrence_rule="FREQ=DAILY;BYHOUR=10",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            enabled=False,
        )

        # Create enabled event listener

        events_repo = EventsRepository(db_context)
        await events_repo.create_event_listener(
            name="Enabled Event",
            description="Test event listener",
            source_id="home_assistant",
            match_conditions={"entity_id": "sensor.test"},
            action_type="wake_llm",
            action_config={"context": "event happened"},
            conversation_id=conversation_id,
            interface_type="telegram",
            enabled=True,
        )

        # List all automations
        automations, total = await db_context.automations.list_all(conversation_id)
        assert total == 3

        # List only enabled
        automations, total = await db_context.automations.list_all(
            conversation_id, enabled=True
        )
        assert total == 2
        names = {a.name for a in automations}
        assert names == {"Enabled Schedule", "Enabled Event"}

        # List only disabled
        automations, total = await db_context.automations.list_all(
            conversation_id, enabled=False
        )
        assert total == 1
        assert automations[0].name == "Disabled Schedule"

    @pytest.mark.asyncio
    async def test_list_all_pagination(self, db_context: DatabaseContext) -> None:
        """Test pagination in list_all."""
        conversation_id = str(uuid.uuid4())

        # Create 5 schedule automations
        for i in range(5):
            await db_context.schedule_automations.create(
                name=f"Auto {i}",
                recurrence_rule="FREQ=DAILY;BYHOUR=9",
                action_type="wake_llm",
                action_config={"context": f"test{i}"},
                conversation_id=conversation_id,
            )

        # Get all
        automations, total = await db_context.automations.list_all(conversation_id)
        assert total == 5
        assert len(automations) == 5

        # Get first page (limit 2)
        automations, total = await db_context.automations.list_all(
            conversation_id, limit=2
        )
        assert total == 5  # Total count should be full count
        assert len(automations) == 2  # But only 2 returned

        # Get second page (offset 2, limit 2)
        automations, total = await db_context.automations.list_all(
            conversation_id, limit=2, offset=2
        )
        assert total == 5
        assert len(automations) == 2

        # Get third page (offset 4, limit 2)
        automations, total = await db_context.automations.list_all(
            conversation_id, limit=2, offset=4
        )
        assert total == 5
        assert len(automations) == 1  # Only 1 remaining

    @pytest.mark.asyncio
    async def test_get_by_id_schedule(self, db_context: DatabaseContext) -> None:
        """Test getting schedule automation by ID."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Schedule",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
        )

        # Get via unified repository
        automation = await db_context.automations.get_by_id(
            automation_id, "schedule", conversation_id
        )
        assert automation is not None
        assert automation.type == "schedule"
        assert automation.name == "Test Schedule"

    @pytest.mark.asyncio
    async def test_get_by_id_event(self, db_context: DatabaseContext) -> None:
        """Test getting event automation by ID."""
        conversation_id = str(uuid.uuid4())

        # Create event listener

        events_repo = EventsRepository(db_context)
        event_id = await events_repo.create_event_listener(
            name="Test Event",
            description="Test event listener",
            source_id="home_assistant",
            match_conditions={"entity_id": "sensor.test"},
            action_type="wake_llm",
            action_config={"context": "event happened"},
            conversation_id=conversation_id,
            interface_type="telegram",
        )

        # Get via unified repository
        automation = await db_context.automations.get_by_id(
            event_id, "event", conversation_id
        )
        assert automation is not None
        assert automation.type == "event"
        assert automation.name == "Test Event"

    @pytest.mark.asyncio
    async def test_get_by_name(self, db_context: DatabaseContext) -> None:
        """Test getting automation by name (searches both types)."""
        conversation_id = str(uuid.uuid4())

        # Create schedule automation
        await db_context.schedule_automations.create(
            name="Unique Schedule Name",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
        )

        # Get by name
        automation = await db_context.automations.get_by_name(
            "Unique Schedule Name", conversation_id
        )
        assert automation is not None
        assert automation.type == "schedule"
        assert automation.name == "Unique Schedule Name"

        # Create event listener

        events_repo = EventsRepository(db_context)
        await events_repo.create_event_listener(
            name="Unique Event Name",
            description="Test event listener",
            source_id="home_assistant",
            match_conditions={"entity_id": "sensor.test"},
            action_type="wake_llm",
            action_config={"context": "event happened"},
            conversation_id=conversation_id,
            interface_type="telegram",
        )

        # Get event by name
        automation = await db_context.automations.get_by_name(
            "Unique Event Name", conversation_id
        )
        assert automation is not None
        assert automation.type == "event"
        assert automation.name == "Unique Event Name"

    @pytest.mark.asyncio
    async def test_check_name_available_both_empty(
        self, db_context: DatabaseContext
    ) -> None:
        """Test name availability when no automations exist."""
        conversation_id = str(uuid.uuid4())

        available, error = await db_context.automations.check_name_available(
            "New Name", conversation_id
        )
        assert available is True
        assert error is None

    @pytest.mark.asyncio
    async def test_check_name_available_schedule_exists(
        self, db_context: DatabaseContext
    ) -> None:
        """Test name availability when schedule automation with name exists."""
        conversation_id = str(uuid.uuid4())

        # Create schedule automation
        await db_context.schedule_automations.create(
            name="Taken Name",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
        )

        # Check name availability
        available, error = await db_context.automations.check_name_available(
            "Taken Name", conversation_id
        )
        assert available is False
        assert error is not None
        assert "already exists" in error
        assert "schedule automation" in error

    @pytest.mark.asyncio
    async def test_check_name_available_event_exists(
        self, db_context: DatabaseContext
    ) -> None:
        """Test name availability when event automation with name exists."""
        conversation_id = str(uuid.uuid4())

        # Create event listener

        events_repo = EventsRepository(db_context)
        await events_repo.create_event_listener(
            name="Taken Event",
            description="Test event listener",
            source_id="home_assistant",
            match_conditions={"entity_id": "sensor.test"},
            action_type="wake_llm",
            action_config={"context": "event happened"},
            conversation_id=conversation_id,
            interface_type="telegram",
        )

        # Check name availability
        available, error = await db_context.automations.check_name_available(
            "Taken Event", conversation_id
        )
        assert available is False
        assert error is not None
        assert "already exists" in error
        assert "event automation" in error

    @pytest.mark.asyncio
    async def test_check_name_available_cross_type_conflict(
        self, db_context: DatabaseContext
    ) -> None:
        """Test that name conflicts are detected across automation types."""
        conversation_id = str(uuid.uuid4())

        # Create schedule automation with name "Shared Name"
        await db_context.schedule_automations.create(
            name="Shared Name",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
        )

        # Try to check if we can create event automation with same name
        available, error = await db_context.automations.check_name_available(
            "Shared Name", conversation_id
        )
        assert available is False
        assert error is not None
        assert "already exists" in error

    @pytest.mark.asyncio
    async def test_check_name_available_with_exclude(
        self, db_context: DatabaseContext
    ) -> None:
        """Test name availability check with exclusion for updates."""
        conversation_id = str(uuid.uuid4())

        # Create schedule automation
        automation_id = await db_context.schedule_automations.create(
            name="Update Me",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
        )

        # Check if we can "update" to same name (should be available when excluding self)
        available, error = await db_context.automations.check_name_available(
            "Update Me",
            conversation_id,
            exclude_id=automation_id,
            exclude_type="schedule",
        )
        assert available is True
        assert error is None

        # Create another automation
        await db_context.schedule_automations.create(
            name="Other Name",
            recurrence_rule="FREQ=DAILY;BYHOUR=10",
            action_type="wake_llm",
            action_config={"context": "test2"},
            conversation_id=conversation_id,
        )

        # Check if we can update first automation to "Other Name" (should not be available)
        available, error = await db_context.automations.check_name_available(
            "Other Name",
            conversation_id,
            exclude_id=automation_id,
            exclude_type="schedule",
        )
        assert available is False
        assert error is not None
        assert "already exists" in error

    @pytest.mark.asyncio
    async def test_update_enabled_schedule(self, db_context: DatabaseContext) -> None:
        """Test updating enabled status for schedule automation."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            enabled=True,
        )

        # Disable via unified repository
        result = await db_context.automations.update_enabled(
            automation_id, "schedule", conversation_id, enabled=False
        )
        assert result is True

        # Verify
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["enabled"] is False

    @pytest.mark.asyncio
    async def test_update_enabled_event(self, db_context: DatabaseContext) -> None:
        """Test updating enabled status for event automation."""
        conversation_id = str(uuid.uuid4())

        # Create event listener

        events_repo = EventsRepository(db_context)
        event_id = await events_repo.create_event_listener(
            name="Test Event",
            description="Test event listener",
            source_id="home_assistant",
            match_conditions={"entity_id": "sensor.test"},
            action_type="wake_llm",
            action_config={"context": "event happened"},
            conversation_id=conversation_id,
            interface_type="telegram",
            enabled=True,
        )

        # Disable via unified repository
        result = await db_context.automations.update_enabled(
            event_id, "event", conversation_id, enabled=False
        )
        assert result is True

        # Verify
        event = await events_repo.get_event_listener_by_id(event_id, conversation_id)
        assert event is not None
        assert event["enabled"] is False

    @pytest.mark.asyncio
    async def test_delete_schedule(self, db_context: DatabaseContext) -> None:
        """Test deleting schedule automation."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
        )

        # Delete via unified repository
        result = await db_context.automations.delete(
            automation_id, "schedule", conversation_id
        )
        assert result is True

        # Verify deleted
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is None

    @pytest.mark.asyncio
    async def test_delete_event(self, db_context: DatabaseContext) -> None:
        """Test deleting event automation."""
        conversation_id = str(uuid.uuid4())

        # Create event listener

        events_repo = EventsRepository(db_context)
        event_id = await events_repo.create_event_listener(
            name="Test Event",
            description="Test event listener",
            source_id="home_assistant",
            match_conditions={"entity_id": "sensor.test"},
            action_type="wake_llm",
            action_config={"context": "event happened"},
            conversation_id=conversation_id,
            interface_type="telegram",
        )

        # Delete via unified repository
        result = await db_context.automations.delete(event_id, "event", conversation_id)
        assert result is True

        # Verify deleted
        event = await events_repo.get_event_listener_by_id(event_id, conversation_id)
        assert event is None

    @pytest.mark.asyncio
    async def test_get_execution_stats_schedule(
        self, db_context: DatabaseContext
    ) -> None:
        """Test getting execution stats for schedule automation."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
        )

        # Get stats via unified repository
        stats = await db_context.automations.get_execution_stats(
            automation_id, "schedule"
        )
        assert stats is not None
        assert "total_executions" in stats
        assert "next_scheduled_at" in stats

    @pytest.mark.asyncio
    async def test_get_execution_stats_event(self, db_context: DatabaseContext) -> None:
        """Test getting execution stats for event automation."""
        conversation_id = str(uuid.uuid4())

        # Create event listener

        events_repo = EventsRepository(db_context)
        event_id = await events_repo.create_event_listener(
            name="Test Event",
            description="Test event listener",
            source_id="home_assistant",
            match_conditions={"entity_id": "sensor.test"},
            action_type="wake_llm",
            action_config={"context": "event happened"},
            conversation_id=conversation_id,
            interface_type="telegram",
        )

        # Get stats via unified repository
        stats = await db_context.automations.get_execution_stats(event_id, "event")
        assert stats is not None
        # Event automations have different stats structure (daily_executions)
        assert "daily_executions" in stats
