"""Functional tests for automations repository complex queries and filtering."""

import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
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


class TestAutomationsRepository:
    """Tests for AutomationsRepository (unified view with complex queries)."""

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
