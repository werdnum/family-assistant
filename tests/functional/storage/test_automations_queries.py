"""Functional tests for automations repository complex queries and filtering."""

import uuid
from collections.abc import AsyncGenerator
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.database import Database
from family_assistant.storage.repositories.events import EventsRepository


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


class TestAutomationsRepository:
    """Tests for AutomationsRepository (unified view with complex queries)."""

    @pytest.mark.asyncio
    async def test_list_all_both_types(self, db_context: Database) -> None:
        """Test listing automations of both types."""
        conversation_id = str(uuid.uuid4())

        # Create schedule automation
        await db_context.schedule_automations.create(
            name="Schedule Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
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
    async def test_list_all_filter_by_type(self, db_context: Database) -> None:
        """Test filtering automations by type."""
        conversation_id = str(uuid.uuid4())

        # Create schedule automation
        await db_context.schedule_automations.create(
            name="Schedule Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
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
    async def test_list_all_filter_by_enabled(self, db_context: Database) -> None:
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
            timezone=ZoneInfo("UTC"),
        )

        # Create disabled schedule automation
        await db_context.schedule_automations.create(
            name="Disabled Schedule",
            recurrence_rule="FREQ=DAILY;BYHOUR=10",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            enabled=False,
            timezone=ZoneInfo("UTC"),
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
    async def test_list_all_pagination(self, db_context: Database) -> None:
        """Pages walk the merged schedule/event list newest-first, without gaps or overlap."""
        conversation_id = str(uuid.uuid4())
        events_repo = EventsRepository(db_context)

        for i in range(5):
            if i % 2 == 0:
                await db_context.schedule_automations.create(
                    name=f"Schedule {i}",
                    recurrence_rule="FREQ=DAILY;BYHOUR=9",
                    action_type="wake_llm",
                    action_config={"context": f"test{i}"},
                    conversation_id=conversation_id,
                    timezone=ZoneInfo("UTC"),
                )
            else:
                await events_repo.create_event_listener(
                    name=f"Event {i}",
                    description="Test event listener",
                    source_id="home_assistant",
                    match_conditions={"entity_id": f"sensor.test{i}"},
                    action_type="wake_llm",
                    action_config={"context": f"event{i}"},
                    conversation_id=conversation_id,
                    interface_type="telegram",
                )

        pages: list[list[str]] = []
        totals: list[int] = []
        for offset in (0, 2, 4):
            automations, total = await db_context.automations.list_all(
                conversation_id, limit=2, offset=offset
            )
            pages.append([a.name for a in automations])
            totals.append(total)

        assert totals == [5, 5, 5]
        assert pages == [
            ["Schedule 4", "Event 3"],
            ["Schedule 2", "Event 1"],
            ["Schedule 0"],
        ]

    @pytest.mark.asyncio
    async def test_get_by_id_schedule(self, db_context: Database) -> None:
        """Test getting schedule automation by ID."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Schedule",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        # Get via unified repository
        automation = await db_context.automations.get_by_id(
            automation_id, "schedule", conversation_id
        )
        assert automation is not None
        assert automation.type == "schedule"
        assert automation.name == "Test Schedule"

    @pytest.mark.asyncio
    async def test_get_by_id_event(self, db_context: Database) -> None:
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
    async def test_get_by_name(self, db_context: Database) -> None:
        """Test getting automation by name (searches both types)."""
        conversation_id = str(uuid.uuid4())

        # Create schedule automation
        await db_context.schedule_automations.create(
            name="Unique Schedule Name",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
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
    async def test_check_name_available_both_empty(self, db_context: Database) -> None:
        """Test name availability when no automations exist."""
        conversation_id = str(uuid.uuid4())

        available, error = await db_context.automations.check_name_available(
            "New Name", conversation_id
        )
        assert available is True
        assert error is None

    @pytest.mark.asyncio
    async def test_check_name_available_schedule_exists(
        self, db_context: Database
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
            timezone=ZoneInfo("UTC"),
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
        self, db_context: Database
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
    async def test_check_name_available_exclusion_of_other_type_with_same_id(
        self, db_context: Database
    ) -> None:
        """Excluding an event does not exempt a schedule that shares its numeric id."""
        conversation_id = str(uuid.uuid4())

        schedule_id = await db_context.schedule_automations.create(
            name="Shared Name",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        available, error = await db_context.automations.check_name_available(
            "Shared Name",
            conversation_id,
            exclude_id=schedule_id,
            exclude_type="event",
        )
        assert available is False
        assert error is not None
        assert "already exists" in error
        assert f"schedule automation ID: {schedule_id}" in error

    @pytest.mark.asyncio
    async def test_check_name_available_with_exclude(
        self, db_context: Database
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
            timezone=ZoneInfo("UTC"),
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
            timezone=ZoneInfo("UTC"),
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
    async def test_update_enabled_schedule(self, db_context: Database) -> None:
        """Test updating enabled status for schedule automation."""
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

        # Disable via unified repository
        result = await db_context.automations.update_enabled(
            automation_id,
            "schedule",
            conversation_id,
            enabled=False,
            timezone=ZoneInfo("UTC"),
        )
        assert result is True

        # Verify
        automation = await db_context.schedule_automations.get_by_id(automation_id)
        assert automation is not None
        assert automation["enabled"] is False

    @pytest.mark.asyncio
    async def test_update_enabled_event(self, db_context: Database) -> None:
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
            event_id, "event", conversation_id, enabled=False, timezone=ZoneInfo("UTC")
        )
        assert result is True

        # Verify
        event = await events_repo.get_event_listener_by_id(event_id, conversation_id)
        assert event is not None
        assert event["enabled"] is False

    @pytest.mark.asyncio
    async def test_delete_schedule(self, db_context: Database) -> None:
        """Test deleting schedule automation."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
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
    async def test_delete_event(self, db_context: Database) -> None:
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
    async def test_get_execution_stats_schedule(self, db_context: Database) -> None:
        """Test getting execution stats for schedule automation."""
        conversation_id = str(uuid.uuid4())

        automation_id = await db_context.schedule_automations.create(
            name="Test Auto",
            recurrence_rule="FREQ=DAILY;BYHOUR=9",
            action_type="wake_llm",
            action_config={"context": "test"},
            conversation_id=conversation_id,
            timezone=ZoneInfo("UTC"),
        )

        stored = await db_context.schedule_automations.get_by_id(automation_id)
        assert stored is not None

        stats = await db_context.automations.get_execution_stats(
            automation_id, "schedule"
        )
        assert stats is not None
        assert "next_scheduled_at" in stats
        assert stats["total_executions"] == 0
        assert stats["last_execution_at"] is None
        assert stats["recent_executions"] == []
        assert stats["next_scheduled_at"] is not None
        assert stats["next_scheduled_at"] == stored["next_scheduled_at"]

    @pytest.mark.asyncio
    async def test_get_execution_stats_event(self, db_context: Database) -> None:
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

        stats = await db_context.automations.get_execution_stats(event_id, "event")
        assert stats is not None
        assert "daily_executions" in stats
        assert stats["total_executions"] == 0
        assert stats["daily_executions"] == 0
        assert stats["last_execution_at"] is None
        assert stats["recent_events"] == []
