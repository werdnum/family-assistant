"""Functional tests for automations CRUD API endpoints.

Tests the /api/automations/event and /api/automations/schedule endpoints
to verify create, read, update, and delete operations for both event and
schedule automations.
"""

import pytest
from httpx import AsyncClient


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
