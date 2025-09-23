"""Functional tests for the event listeners API endpoints.

Tests the /api/event-listeners endpoints to verify condition_script field
is properly handled in all CRUD operations.
"""

import pytest
from httpx import AsyncClient

# Use the shared API test fixtures from conftest.py


# --- Test Classes ---


@pytest.mark.asyncio
class TestEventListenersAPI:
    """Test suite for the event listeners API endpoints."""

    async def test_create_listener_with_condition_script(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test creating an event listener with condition_script field."""
        listener_data = {
            "name": "Test Script Listener",
            "source_id": "home_assistant",
            "action_type": "wake_llm",
            "match_conditions": {"entity_id": "sensor.test"},
            "condition_script": "return event.get('new_state', {}).get('state') == 'active'",
            "description": "Test listener with script condition",
            "conversation_id": "test_api",
        }

        response = await api_test_client.post(
            "/api/event-listeners", json=listener_data
        )

        assert response.status_code == 200
        data = response.json()

        # Verify all fields are returned including condition_script
        assert data["name"] == listener_data["name"]
        assert data["source_id"] == listener_data["source_id"]
        assert data["action_type"] == listener_data["action_type"]
        assert data["match_conditions"] == listener_data["match_conditions"]
        assert data["condition_script"] == listener_data["condition_script"]
        assert data["description"] == listener_data["description"]
        assert data["conversation_id"] == listener_data["conversation_id"]
        assert "id" in data
        assert data["enabled"] is True

    async def test_create_listener_without_condition_script(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test creating an event listener without condition_script field."""
        listener_data = {
            "name": "Test JSON Only Listener",
            "source_id": "indexing",
            "action_type": "script",
            "match_conditions": {"document_type": "pdf"},
            "action_config": {"script_code": "print('processed')"},
            "description": "Test listener with only JSON conditions",
            "conversation_id": "test_api",
        }

        response = await api_test_client.post(
            "/api/event-listeners", json=listener_data
        )

        assert response.status_code == 200
        data = response.json()

        # Verify condition_script is None when not provided
        assert data["condition_script"] is None
        assert data["match_conditions"] == listener_data["match_conditions"]

    async def test_get_listener_includes_condition_script(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test that GET endpoint returns condition_script field."""
        # First create a listener with condition_script
        listener_data = {
            "name": "Get Test Listener",
            "source_id": "webhook",
            "action_type": "wake_llm",
            "match_conditions": {"event_type": "push"},
            "condition_script": "return event.get('repository', {}).get('name') == 'main-repo'",
            "conversation_id": "test_api",
        }

        create_response = await api_test_client.post(
            "/api/event-listeners", json=listener_data
        )
        assert create_response.status_code == 200
        created_data = create_response.json()
        listener_id = created_data["id"]

        # Now get the listener by ID
        get_response = await api_test_client.get(f"/api/event-listeners/{listener_id}")
        assert get_response.status_code == 200
        data = get_response.json()

        # Verify condition_script is included and matches
        assert data["condition_script"] == listener_data["condition_script"]
        assert data["match_conditions"] == listener_data["match_conditions"]
        assert data["id"] == listener_id

    async def test_list_listeners_includes_condition_script(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test that LIST endpoint returns condition_script field."""
        # Create listeners with and without condition_script
        listener_with_script = {
            "name": "List Test With Script",
            "source_id": "home_assistant",
            "action_type": "wake_llm",
            "match_conditions": {"entity_id": "light.kitchen"},
            "condition_script": "return event.get('new_state', {}).get('state') == 'on'",
            "conversation_id": "test_api",
        }

        listener_without_script = {
            "name": "List Test Without Script",
            "source_id": "home_assistant",
            "action_type": "wake_llm",
            "match_conditions": {"entity_id": "light.living_room"},
            "conversation_id": "test_api",
        }

        # Create both listeners
        await api_test_client.post("/api/event-listeners", json=listener_with_script)
        await api_test_client.post("/api/event-listeners", json=listener_without_script)

        # List all listeners
        list_response = await api_test_client.get("/api/event-listeners")
        assert list_response.status_code == 200
        data = list_response.json()

        assert "listeners" in data
        listeners = data["listeners"]
        assert len(listeners) >= 2

        # Find our test listeners and verify condition_script handling
        test_listeners = [
            listener
            for listener in listeners
            if listener["name"].startswith("List Test")
            and listener["conversation_id"] == "test_api"
        ]
        assert len(test_listeners) == 2

        # Verify one has condition_script and one doesn't
        script_listener = next(
            listener
            for listener in test_listeners
            if listener["name"] == "List Test With Script"
        )
        no_script_listener = next(
            listener
            for listener in test_listeners
            if listener["name"] == "List Test Without Script"
        )

        assert (
            script_listener["condition_script"]
            == listener_with_script["condition_script"]
        )
        assert no_script_listener["condition_script"] is None

    async def test_update_listener_condition_script(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test updating an event listener's condition_script field."""
        # Create initial listener without condition_script
        initial_data = {
            "name": "Update Test Listener",
            "source_id": "indexing",
            "action_type": "wake_llm",
            "match_conditions": {"status": "complete"},
            "conversation_id": "test_api",
        }

        create_response = await api_test_client.post(
            "/api/event-listeners", json=initial_data
        )
        assert create_response.status_code == 200
        listener_id = create_response.json()["id"]

        # Update to add condition_script
        update_data = {"condition_script": "return event.get('document_count', 0) > 5"}

        update_response = await api_test_client.patch(
            f"/api/event-listeners/{listener_id}?conversation_id=test_api",
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
        # Note: The API doesn't support setting to None - use empty string to clear
        clear_update = {"condition_script": ""}
        clear_response = await api_test_client.patch(
            f"/api/event-listeners/{listener_id}?conversation_id=test_api",
            json=clear_update,
        )
        assert clear_response.status_code == 200
        cleared_data = clear_response.json()

        # Verify condition_script was cleared (API returns empty string, not None)
        assert not cleared_data["condition_script"]
        assert not cleared_data["condition_script"]
    async def test_delete_listener(self, api_test_client: AsyncClient) -> None:
        """Test deleting an event listener with condition_script."""
        # Create initial listener with condition_script
        initial_data = {
            "name": "Delete Test Listener",
            "source_id": "webhook",
            "action_type": "wake_llm",
            "match_conditions": {"event_type": "push"},
            "condition_script": "return event.get('action') == 'opened'",
            "conversation_id": "test_api",
        }

        create_response = await api_test_client.post(
            "/api/event-listeners", json=initial_data
        )
        assert create_response.status_code == 200
        listener_id = create_response.json()["id"]

        # Delete the listener
        delete_response = await api_test_client.delete(
            f"/api/event-listeners/{listener_id}?conversation_id=test_api"
        )
        assert delete_response.status_code == 200

        # Verify the listener no longer exists
        get_response = await api_test_client.get(f"/api/event-listeners/{listener_id}")
        assert get_response.status_code == 404

    async def test_create_listener_with_both_conditions_stores_both(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test creating a listener with both match_conditions and condition_script."""
        listener_data = {
            "name": "Both Conditions Listener",
            "source_id": "home_assistant",
            "action_type": "wake_llm",
            "match_conditions": {"entity_id": "binary_sensor.door"},
            "condition_script": """
# Complex condition that can't be expressed in JSON
old_state = event.get('old_state', {}).get('state')
new_state = event.get('new_state', {}).get('state')
return old_state == 'off' and new_state == 'on'
""".strip(),
            "description": "Listener with both JSON and script conditions",
            "conversation_id": "test_api",
        }

        response = await api_test_client.post(
            "/api/event-listeners", json=listener_data
        )
        assert response.status_code == 200
        data = response.json()

        # Both should be stored and returned
        assert data["match_conditions"] == listener_data["match_conditions"]
        assert data["condition_script"] == listener_data["condition_script"]

        # According to the system design, script takes precedence over JSON conditions
        # This test just verifies both are stored - the precedence logic is in the processor

    async def test_api_error_handling_for_invalid_condition_script(
        self, api_test_client: AsyncClient
    ) -> None:
        """Test that invalid condition_script is handled gracefully."""
        # Note: The API layer doesn't validate Starlark syntax - that's done at execution time
        # This test just verifies the field is accepted and stored
        listener_data = {
            "name": "Invalid Script Listener",
            "source_id": "webhook",
            "action_type": "wake_llm",
            "match_conditions": {"type": "test"},
            "condition_script": "this is not valid starlark syntax !!!",
            "conversation_id": "test_api",
        }

        response = await api_test_client.post(
            "/api/event-listeners", json=listener_data
        )

        # API should accept it (validation happens at execution time)
        assert response.status_code == 200
        data = response.json()
        assert data["condition_script"] == listener_data["condition_script"]
