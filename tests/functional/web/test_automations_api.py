"""Functional tests for the unified automations API endpoints.

Tests the /api/automations endpoints to verify CRUD operations, pagination,
filtering, conversation scoping, and task cancellation for both event and
schedule automations.
"""

import pytest
from httpx import AsyncClient

# --- Test Classes ---


@pytest.mark.asyncio
class TestEventAutomationsAPI:
    """Test suite for event automation API endpoints."""

    async def test_create_event_automation_with_condition_script(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test creating an event automation with condition_script field."""
        automation_data = {
            "name": "Test Event Automation With Script",
            "source_id": "home_assistant",
            "action_type": "wake_llm",
            "match_conditions": {"entity_id": "sensor.test"},
            "condition_script": "return event.get('new_state', {}).get('state') == 'active'",
            "description": "Test event automation with script condition",
            "conversation_id": "test_api",
        }

        response = await api_test_client.post(
            "/api/automations/event", json=automation_data
        )

        assert response.status_code == 200
        data = response.json()

        # Verify all fields are returned including condition_script
        assert data["type"] == "event"
        assert data["name"] == automation_data["name"]
        assert data["source_id"] == automation_data["source_id"]
        assert data["action_type"] == automation_data["action_type"]
        assert data["match_conditions"] == automation_data["match_conditions"]
        assert data["condition_script"] == automation_data["condition_script"]
        assert data["description"] == automation_data["description"]
        assert data["conversation_id"] == automation_data["conversation_id"]
        assert "id" in data
        assert data["enabled"] is True

    async def test_create_event_automation_without_condition_script(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test creating an event automation without condition_script field."""
        automation_data = {
            "name": "Test Event Automation Without Script",
            "source_id": "indexing",
            "action_type": "script",
            "match_conditions": {"document_type": "pdf"},
            "action_config": {"script_code": "print('processed')"},
            "description": "Test event automation with only JSON conditions",
            "conversation_id": "test_api",
        }

        response = await api_test_client.post(
            "/api/automations/event", json=automation_data
        )

        assert response.status_code == 200
        data = response.json()

        # Verify condition_script is None when not provided
        assert data["condition_script"] is None
        assert data["match_conditions"] == automation_data["match_conditions"]

    async def test_create_event_automation_validates_source_id(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test that invalid source_id is rejected."""
        automation_data = {
            "name": "Test Invalid Source",
            "source_id": "invalid_source",
            "action_type": "wake_llm",
            "match_conditions": {"entity_id": "sensor.test"},
            "conversation_id": "test_api",
        }

        response = await api_test_client.post(
            "/api/automations/event", json=automation_data
        )

        assert response.status_code == 400
        assert "Invalid source_id" in response.json()["detail"]

    async def test_create_event_automation_validates_action_type(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test that invalid action_type is rejected."""
        automation_data = {
            "name": "Test Invalid Action",
            "source_id": "home_assistant",
            "action_type": "invalid_action",
            "match_conditions": {"entity_id": "sensor.test"},
            "conversation_id": "test_api",
        }

        response = await api_test_client.post(
            "/api/automations/event", json=automation_data
        )

        assert response.status_code == 400
        assert "Invalid action_type" in response.json()["detail"]

    async def test_create_event_automation_script_action_requires_script_code(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test that script action type requires script_code in action_config."""
        automation_data = {
            "name": "Test Script Without Code",
            "source_id": "webhook",
            "action_type": "script",
            "match_conditions": {"event_type": "push"},
            "action_config": {},  # Missing script_code
            "conversation_id": "test_api",
        }

        response = await api_test_client.post(
            "/api/automations/event", json=automation_data
        )

        assert response.status_code == 400
        assert "script_code is required" in response.json()["detail"]

    async def test_get_event_automation_includes_condition_script(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test that GET endpoint returns condition_script field."""
        # First create an event automation with condition_script
        automation_data = {
            "name": "Test Get Event Automation",
            "source_id": "webhook",
            "action_type": "wake_llm",
            "match_conditions": {"event_type": "push"},
            "condition_script": "return event.get('repository', {}).get('name') == 'main-repo'",
            "conversation_id": "test_api",
        }

        create_response = await api_test_client.post(
            "/api/automations/event", json=automation_data
        )
        assert create_response.status_code == 200
        created_data = create_response.json()
        automation_id = created_data["id"]

        # Now get the automation by ID
        get_response = await api_test_client.get(
            f"/api/automations/event/{automation_id}?conversation_id=test_api"
        )
        assert get_response.status_code == 200
        data = get_response.json()

        # Verify condition_script is included and matches
        assert data["condition_script"] == automation_data["condition_script"]
        assert data["match_conditions"] == automation_data["match_conditions"]
        assert data["id"] == automation_id

    async def test_update_event_automation_condition_script(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test updating an event automation's condition_script field."""
        # Create initial automation without condition_script
        initial_data = {
            "name": "Test Update Event Automation",
            "source_id": "indexing",
            "action_type": "wake_llm",
            "match_conditions": {"status": "complete"},
            "conversation_id": "test_api",
        }

        create_response = await api_test_client.post(
            "/api/automations/event", json=initial_data
        )
        assert create_response.status_code == 200
        automation_id = create_response.json()["id"]

        # Update to add condition_script
        update_data = {"condition_script": "return event.get('document_count', 0) > 5"}

        update_response = await api_test_client.patch(
            f"/api/automations/event/{automation_id}?conversation_id=test_api",
            json=update_data,
        )
        assert update_response.status_code == 200
        updated_data = update_response.json()

        # Verify condition_script was added
        assert updated_data["condition_script"] == update_data["condition_script"]
        assert (
            updated_data["match_conditions"] == initial_data["match_conditions"]
        )  # Unchanged

        # Update to clear condition_script (set to empty string)
        clear_update = {"condition_script": ""}
        clear_response = await api_test_client.patch(
            f"/api/automations/event/{automation_id}?conversation_id=test_api",
            json=clear_update,
        )
        assert clear_response.status_code == 200
        cleared_data = clear_response.json()

        # Verify condition_script was cleared
        assert not cleared_data["condition_script"]

    async def test_delete_event_automation(self, api_test_client: AsyncClient) -> None:
        """Test deleting an event automation."""
        # Create an event automation
        automation_data = {
            "name": "Test Delete Event Automation",
            "source_id": "webhook",
            "action_type": "wake_llm",
            "match_conditions": {"event_type": "push"},
            "condition_script": "return event.get('action') == 'opened'",
            "conversation_id": "test_api",
        }

        create_response = await api_test_client.post(
            "/api/automations/event", json=automation_data
        )
        assert create_response.status_code == 200
        automation_id = create_response.json()["id"]

        # Delete the automation
        delete_response = await api_test_client.delete(
            f"/api/automations/event/{automation_id}?conversation_id=test_api"
        )
        assert delete_response.status_code == 200

        # Verify the automation no longer exists
        get_response = await api_test_client.get(
            f"/api/automations/event/{automation_id}?conversation_id=test_api"
        )
        assert get_response.status_code == 404

    async def test_enable_disable_event_automation(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test enabling and disabling an event automation."""
        # Create an event automation
        automation_data = {
            "name": "Test Enable/Disable Event Automation",
            "source_id": "home_assistant",
            "action_type": "wake_llm",
            "match_conditions": {"entity_id": "light.kitchen"},
            "conversation_id": "test_api",
            "enabled": True,
        }

        create_response = await api_test_client.post(
            "/api/automations/event", json=automation_data
        )
        assert create_response.status_code == 200
        automation_id = create_response.json()["id"]
        assert create_response.json()["enabled"] is True

        # Disable the automation
        disable_response = await api_test_client.patch(
            f"/api/automations/event/{automation_id}/enabled?conversation_id=test_api&enabled=false"
        )
        assert disable_response.status_code == 200
        assert disable_response.json()["enabled"] is False

        # Enable the automation
        enable_response = await api_test_client.patch(
            f"/api/automations/event/{automation_id}/enabled?conversation_id=test_api&enabled=true"
        )
        assert enable_response.status_code == 200
        assert enable_response.json()["enabled"] is True


@pytest.mark.asyncio
class TestScheduleAutomationsAPI:
    """Test suite for schedule automation API endpoints."""

    async def test_create_schedule_automation(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test creating a schedule automation."""
        automation_data = {
            "name": "Test Schedule Automation",
            "recurrence_rule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            "action_type": "wake_llm",
            "action_config": {"message": "Good morning!"},
            "description": "Daily morning reminder",
            "conversation_id": "test_api",
        }

        response = await api_test_client.post(
            "/api/automations/schedule", json=automation_data
        )

        assert response.status_code == 200
        data = response.json()

        # Verify all fields are returned
        assert data["type"] == "schedule"
        assert data["name"] == automation_data["name"]
        assert data["recurrence_rule"] == automation_data["recurrence_rule"]
        assert data["action_type"] == automation_data["action_type"]
        assert data["action_config"] == automation_data["action_config"]
        assert data["description"] == automation_data["description"]
        assert data["conversation_id"] == automation_data["conversation_id"]
        assert "id" in data
        assert data["enabled"] is True
        assert data["next_scheduled_at"] is not None

    async def test_create_schedule_automation_validates_action_type(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test that invalid action_type is rejected for schedule automations."""
        automation_data = {
            "name": "Test Invalid Schedule Action",
            "recurrence_rule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            "action_type": "invalid_action",
            "conversation_id": "test_api",
        }

        response = await api_test_client.post(
            "/api/automations/schedule", json=automation_data
        )

        assert response.status_code == 400
        assert "Invalid action_type" in response.json()["detail"]

    async def test_create_schedule_automation_script_action_requires_script_code(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test that script action type requires script_code in action_config."""
        automation_data = {
            "name": "Test Schedule Script Without Code",
            "recurrence_rule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            "action_type": "script",
            "action_config": {},  # Missing script_code
            "conversation_id": "test_api",
        }

        response = await api_test_client.post(
            "/api/automations/schedule", json=automation_data
        )

        assert response.status_code == 400
        assert "script_code is required" in response.json()["detail"]

    async def test_get_schedule_automation(self, api_test_client: AsyncClient) -> None:
        """Test that GET endpoint returns schedule automation details."""
        # First create a schedule automation
        automation_data = {
            "name": "Test Get Schedule Automation",
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO",
            "action_type": "wake_llm",
            "action_config": {"message": "Weekly report time!"},
            "conversation_id": "test_api",
        }

        create_response = await api_test_client.post(
            "/api/automations/schedule", json=automation_data
        )
        assert create_response.status_code == 200
        created_data = create_response.json()
        automation_id = created_data["id"]

        # Now get the automation by ID
        get_response = await api_test_client.get(
            f"/api/automations/schedule/{automation_id}?conversation_id=test_api"
        )
        assert get_response.status_code == 200
        data = get_response.json()

        # Verify fields match
        assert data["recurrence_rule"] == automation_data["recurrence_rule"]
        assert data["action_config"] == automation_data["action_config"]
        assert data["id"] == automation_id

    async def test_update_schedule_automation_recurrence_rule(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test updating a schedule automation's recurrence_rule."""
        # Create initial automation
        initial_data = {
            "name": "Test Update Schedule Automation",
            "recurrence_rule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            "action_type": "wake_llm",
            "action_config": {"message": "Morning!"},
            "conversation_id": "test_api",
        }

        create_response = await api_test_client.post(
            "/api/automations/schedule", json=initial_data
        )
        assert create_response.status_code == 200
        automation_id = create_response.json()["id"]

        # Update recurrence_rule
        update_data = {"recurrence_rule": "FREQ=DAILY;BYHOUR=18;BYMINUTE=0"}

        update_response = await api_test_client.patch(
            f"/api/automations/schedule/{automation_id}?conversation_id=test_api",
            json=update_data,
        )
        assert update_response.status_code == 200
        updated_data = update_response.json()

        # Verify recurrence_rule was updated
        assert updated_data["recurrence_rule"] == update_data["recurrence_rule"]

    async def test_delete_schedule_automation(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test deleting a schedule automation."""
        # Create a schedule automation
        automation_data = {
            "name": "Test Delete Schedule Automation",
            "recurrence_rule": "FREQ=DAILY;BYHOUR=10;BYMINUTE=0",
            "action_type": "wake_llm",
            "action_config": {"message": "Time to delete!"},
            "conversation_id": "test_api",
        }

        create_response = await api_test_client.post(
            "/api/automations/schedule", json=automation_data
        )
        assert create_response.status_code == 200
        automation_id = create_response.json()["id"]

        # Delete the automation
        delete_response = await api_test_client.delete(
            f"/api/automations/schedule/{automation_id}?conversation_id=test_api"
        )
        assert delete_response.status_code == 200

        # Verify the automation no longer exists
        get_response = await api_test_client.get(
            f"/api/automations/schedule/{automation_id}?conversation_id=test_api"
        )
        assert get_response.status_code == 404

    async def test_enable_disable_schedule_automation(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test enabling and disabling a schedule automation."""
        # Create a schedule automation
        automation_data = {
            "name": "Test Enable/Disable Schedule Automation",
            "recurrence_rule": "FREQ=DAILY;BYHOUR=12;BYMINUTE=0",
            "action_type": "wake_llm",
            "action_config": {"message": "Noon notification"},
            "conversation_id": "test_api",
            "enabled": True,
        }

        create_response = await api_test_client.post(
            "/api/automations/schedule", json=automation_data
        )
        assert create_response.status_code == 200
        automation_id = create_response.json()["id"]
        assert create_response.json()["enabled"] is True

        # Disable the automation
        disable_response = await api_test_client.patch(
            f"/api/automations/schedule/{automation_id}/enabled?conversation_id=test_api&enabled=false"
        )
        assert disable_response.status_code == 200
        assert disable_response.json()["enabled"] is False

        # Enable the automation
        enable_response = await api_test_client.patch(
            f"/api/automations/schedule/{automation_id}/enabled?conversation_id=test_api&enabled=true"
        )
        assert enable_response.status_code == 200
        assert enable_response.json()["enabled"] is True


@pytest.mark.asyncio
class TestUnifiedAutomationsAPI:
    """Test suite for unified automations API features."""

    async def test_list_automations_includes_both_types(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test that LIST endpoint returns both event and schedule automations."""
        # Create an event automation
        event_data = {
            "name": "Test Event For List",
            "source_id": "home_assistant",
            "action_type": "wake_llm",
            "match_conditions": {"entity_id": "light.kitchen"},
            "conversation_id": "test_api_list",
        }
        await api_test_client.post("/api/automations/event", json=event_data)

        # Create a schedule automation
        schedule_data = {
            "name": "Test Schedule For List",
            "recurrence_rule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            "action_type": "wake_llm",
            "action_config": {"message": "Morning!"},
            "conversation_id": "test_api_list",
        }
        await api_test_client.post("/api/automations/schedule", json=schedule_data)

        # List all automations
        list_response = await api_test_client.get(
            "/api/automations?conversation_id=test_api_list"
        )
        assert list_response.status_code == 200
        data = list_response.json()

        assert "automations" in data
        automations = data["automations"]
        assert len(automations) >= 2

        # Find our test automations
        test_automations = [
            a
            for a in automations
            if a["name"].startswith("Test") and a["conversation_id"] == "test_api_list"
        ]
        assert len(test_automations) == 2

        # Verify one is event and one is schedule
        types = {a["type"] for a in test_automations}
        assert types == {"event", "schedule"}

    async def test_list_automations_filter_by_type(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test filtering automations by type."""
        # Create an event automation
        event_data = {
            "name": "Test Event For Type Filter",
            "source_id": "webhook",
            "action_type": "wake_llm",
            "match_conditions": {"event_type": "push"},
            "conversation_id": "test_api_filter",
        }
        await api_test_client.post("/api/automations/event", json=event_data)

        # Create a schedule automation
        schedule_data = {
            "name": "Test Schedule For Type Filter",
            "recurrence_rule": "FREQ=DAILY;BYHOUR=10;BYMINUTE=0",
            "action_type": "wake_llm",
            "action_config": {"message": "Mid-morning!"},
            "conversation_id": "test_api_filter",
        }
        await api_test_client.post("/api/automations/schedule", json=schedule_data)

        # Filter by event type
        event_response = await api_test_client.get(
            "/api/automations?conversation_id=test_api_filter&automation_type=event"
        )
        assert event_response.status_code == 200
        event_data_response = event_response.json()
        event_automations = [
            a
            for a in event_data_response["automations"]
            if a["conversation_id"] == "test_api_filter"
        ]
        assert all(a["type"] == "event" for a in event_automations)

        # Filter by schedule type
        schedule_response = await api_test_client.get(
            "/api/automations?conversation_id=test_api_filter&automation_type=schedule"
        )
        assert schedule_response.status_code == 200
        schedule_data_response = schedule_response.json()
        schedule_automations = [
            a
            for a in schedule_data_response["automations"]
            if a["conversation_id"] == "test_api_filter"
        ]
        assert all(a["type"] == "schedule" for a in schedule_automations)

    async def test_list_automations_filter_by_enabled(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test filtering automations by enabled status."""
        # Create enabled automation
        enabled_data = {
            "name": "Test Enabled Automation",
            "source_id": "home_assistant",
            "action_type": "wake_llm",
            "match_conditions": {"entity_id": "sensor.enabled"},
            "conversation_id": "test_api_enabled",
            "enabled": True,
        }
        await api_test_client.post("/api/automations/event", json=enabled_data)

        # Create disabled automation
        disabled_data = {
            "name": "Test Disabled Automation",
            "source_id": "home_assistant",
            "action_type": "wake_llm",
            "match_conditions": {"entity_id": "sensor.disabled"},
            "conversation_id": "test_api_enabled",
            "enabled": False,
        }
        await api_test_client.post("/api/automations/event", json=disabled_data)

        # Filter by enabled=true
        enabled_response = await api_test_client.get(
            "/api/automations?conversation_id=test_api_enabled&enabled=true"
        )
        assert enabled_response.status_code == 200
        enabled_automations = [
            a
            for a in enabled_response.json()["automations"]
            if a["conversation_id"] == "test_api_enabled"
        ]
        assert all(a["enabled"] is True for a in enabled_automations)

        # Filter by enabled=false
        disabled_response = await api_test_client.get(
            "/api/automations?conversation_id=test_api_enabled&enabled=false"
        )
        assert disabled_response.status_code == 200
        disabled_automations = [
            a
            for a in disabled_response.json()["automations"]
            if a["conversation_id"] == "test_api_enabled"
        ]
        assert all(a["enabled"] is False for a in disabled_automations)

    async def test_list_automations_pagination(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test pagination of automations list."""
        # Create multiple automations
        for i in range(5):
            event_data = {
                "name": f"Test Event Pagination {i}",
                "source_id": "webhook",
                "action_type": "wake_llm",
                "match_conditions": {"index": i},
                "conversation_id": "test_api_pagination",
            }
            await api_test_client.post("/api/automations/event", json=event_data)

        # Get first page
        page1_response = await api_test_client.get(
            "/api/automations?conversation_id=test_api_pagination&page=1&page_size=2"
        )
        assert page1_response.status_code == 200
        page1_data = page1_response.json()

        assert page1_data["page"] == 1
        assert page1_data["page_size"] == 2
        assert page1_data["total_count"] >= 5
        page1_automations = [
            a
            for a in page1_data["automations"]
            if a["conversation_id"] == "test_api_pagination"
        ]
        assert len(page1_automations) <= 2

    async def test_cross_type_name_uniqueness(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test that names must be unique across event and schedule automations."""
        # Create an event automation with a specific name
        event_data = {
            "name": "Test Unique Name",
            "source_id": "home_assistant",
            "action_type": "wake_llm",
            "match_conditions": {"entity_id": "sensor.test"},
            "conversation_id": "test_api_unique",
        }
        event_response = await api_test_client.post(
            "/api/automations/event", json=event_data
        )
        assert event_response.status_code == 200

        # Try to create a schedule automation with the same name
        schedule_data = {
            "name": "Test Unique Name",  # Same name
            "recurrence_rule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            "action_type": "wake_llm",
            "action_config": {"message": "Morning!"},
            "conversation_id": "test_api_unique",
        }
        schedule_response = await api_test_client.post(
            "/api/automations/schedule", json=schedule_data
        )
        assert schedule_response.status_code == 400
        assert "already exists" in schedule_response.json()["detail"]

    async def test_conversation_scoping(self, api_test_client: AsyncClient) -> None:
        """Test that automations are properly scoped to conversations."""
        # Create automation in conversation A
        automation_a = {
            "name": "Test Conv A Automation",
            "source_id": "webhook",
            "action_type": "wake_llm",
            "match_conditions": {"event_type": "push"},
            "conversation_id": "conversation_a",
        }
        create_a_response = await api_test_client.post(
            "/api/automations/event", json=automation_a
        )
        assert create_a_response.status_code == 200
        automation_a_id = create_a_response.json()["id"]

        # Create automation in conversation B with the same name (should be allowed)
        automation_b = {
            "name": "Test Conv A Automation",  # Same name, different conversation
            "source_id": "webhook",
            "action_type": "wake_llm",
            "match_conditions": {"event_type": "push"},
            "conversation_id": "conversation_b",
        }
        create_b_response = await api_test_client.post(
            "/api/automations/event", json=automation_b
        )
        assert create_b_response.status_code == 200

        # Try to access automation A from conversation B (should fail)
        get_response = await api_test_client.get(
            f"/api/automations/event/{automation_a_id}?conversation_id=conversation_b"
        )
        assert get_response.status_code == 404

        # List automations in conversation A (should only see automation A)
        list_a_response = await api_test_client.get(
            "/api/automations?conversation_id=conversation_a"
        )
        assert list_a_response.status_code == 200
        conv_a_automations = [
            a
            for a in list_a_response.json()["automations"]
            if a["conversation_id"] == "conversation_a"
        ]
        assert len(conv_a_automations) == 1

    async def test_invalid_automation_type_in_path(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test that invalid automation type in path is rejected."""
        response = await api_test_client.get(
            "/api/automations/invalid_type/123?conversation_id=test_api"
        )
        assert response.status_code == 400
        assert "must be 'event' or 'schedule'" in response.json()["detail"]

    async def test_get_automation_stats(self, api_test_client: AsyncClient) -> None:
        """Test getting execution statistics for an automation."""
        # Create an automation
        automation_data = {
            "name": "Test Stats Automation",
            "source_id": "webhook",
            "action_type": "wake_llm",
            "match_conditions": {"event_type": "push"},
            "conversation_id": "test_api_stats",
        }
        create_response = await api_test_client.post(
            "/api/automations/event", json=automation_data
        )
        assert create_response.status_code == 200
        automation_id = create_response.json()["id"]

        # Get stats
        stats_response = await api_test_client.get(
            f"/api/automations/event/{automation_id}/stats?conversation_id=test_api_stats"
        )
        assert stats_response.status_code == 200
        stats = stats_response.json()

        # Verify stats structure
        assert "daily_executions" in stats or "execution_count" in stats

    async def test_update_automation_name_uniqueness(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test that updating automation name checks for uniqueness."""
        # Create two automations
        automation1_data = {
            "name": "Test Update Name 1",
            "source_id": "webhook",
            "action_type": "wake_llm",
            "match_conditions": {"event_type": "push"},
            "conversation_id": "test_api_update",
        }
        create1_response = await api_test_client.post(
            "/api/automations/event", json=automation1_data
        )
        assert create1_response.status_code == 200
        automation1_id = create1_response.json()["id"]

        automation2_data = {
            "name": "Test Update Name 2",
            "source_id": "webhook",
            "action_type": "wake_llm",
            "match_conditions": {"event_type": "pull_request"},
            "conversation_id": "test_api_update",
        }
        create2_response = await api_test_client.post(
            "/api/automations/event", json=automation2_data
        )
        assert create2_response.status_code == 200

        # Try to update automation1 to have the same name as automation2
        update_data = {"name": "Test Update Name 2"}
        update_response = await api_test_client.patch(
            f"/api/automations/event/{automation1_id}?conversation_id=test_api_update",
            json=update_data,
        )
        assert update_response.status_code == 400
        assert "already exists" in update_response.json()["detail"]

    async def test_event_automation_with_both_conditions(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test creating an automation with both match_conditions and condition_script."""
        automation_data = {
            "name": "Test Both Conditions",
            "source_id": "home_assistant",
            "action_type": "wake_llm",
            "match_conditions": {"entity_id": "binary_sensor.door"},
            "condition_script": """
# Complex condition that can't be expressed in JSON
old_state = event.get('old_state', {}).get('state')
new_state = event.get('new_state', {}).get('state')
return old_state == 'off' and new_state == 'on'
""".strip(),
            "description": "Event automation with both JSON and script conditions",
            "conversation_id": "test_api",
        }

        response = await api_test_client.post(
            "/api/automations/event", json=automation_data
        )
        assert response.status_code == 200
        data = response.json()

        # Both should be stored and returned
        assert data["match_conditions"] == automation_data["match_conditions"]
        assert data["condition_script"] == automation_data["condition_script"]
