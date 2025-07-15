"""
Functional tests for event listener validation with Home Assistant.

Tests the actual validation behavior when creating event listeners
with invalid entity IDs, using real event source but mocked HA client.
"""

import json
from unittest.mock import Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.events.home_assistant_source import HomeAssistantSource
from family_assistant.events.processor import EventProcessor
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.event_listeners import create_event_listener_tool
from family_assistant.tools.types import ToolExecutionContext


class TestEventListenerValidationFunctional:
    """Functional tests for event listener validation."""

    @pytest.fixture
    async def mock_ha_client(self) -> Mock:
        """Create a mock Home Assistant client with test entities."""
        client = Mock()
        # Mock get_states to return known entities
        mock_states = [
            Mock(entity_id="person.alex_smith"),
            Mock(entity_id="person.taylor_smith"),
            Mock(entity_id="light.living_room"),
            Mock(entity_id="switch.garage"),
            Mock(entity_id="sensor.temperature"),
        ]
        client.get_states = Mock(return_value=mock_states)
        return client

    @pytest.fixture
    async def event_processor_with_ha(self, mock_ha_client: Mock) -> EventProcessor:
        """Create an event processor with Home Assistant source."""
        ha_source = HomeAssistantSource(mock_ha_client)
        processor = EventProcessor(sources={"home_assistant": ha_source})
        return processor

    @pytest.mark.asyncio
    async def test_create_listener_with_invalid_entity_format(
        self, db_engine: AsyncEngine, event_processor_with_ha: EventProcessor
    ) -> None:
        """Test that creating a listener with invalid entity format fails with validation error."""
        async with DatabaseContext(engine=db_engine) as db_ctx:
            # Create execution context
            exec_context = ToolExecutionContext(
                interface_type="telegram",
                conversation_id="test_validation",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_ctx,
            )

            # Set event sources for validation
            exec_context.event_sources = event_processor_with_ha.sources

            # Try to create listener with invalid entity format
            result = await create_event_listener_tool(
                exec_context=exec_context,
                name="Invalid Entity Format Test",
                source="home_assistant",
                listener_config={
                    "match_conditions": {
                        "entity_id": "invalid-entity",  # Invalid format (dash)
                    }
                },
            )

            # Parse result
            data = json.loads(result)
            assert data["success"] is False
            assert data["message"] == "Validation failed"
            assert "validation_errors" in data
            assert len(data["validation_errors"]) > 0

            # Check the validation error details
            error = data["validation_errors"][0]
            assert error["field"] == "entity_id"
            assert "Invalid entity ID format" in error["error"]
            assert error["suggestion"] is not None

    @pytest.mark.asyncio
    async def test_create_listener_with_nonexistent_entity(
        self, db_engine: AsyncEngine, event_processor_with_ha: EventProcessor
    ) -> None:
        """Test that creating a listener with non-existent entity fails with helpful suggestion."""
        async with DatabaseContext(engine=db_engine) as db_ctx:
            # Create execution context
            exec_context = ToolExecutionContext(
                interface_type="telegram",
                conversation_id="test_validation",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_ctx,
            )

            # Set event sources for validation
            exec_context.event_sources = event_processor_with_ha.sources

            # Try to create listener with shortened person entity
            result = await create_event_listener_tool(
                exec_context=exec_context,
                name="Shortened Entity Test",
                source="home_assistant",
                listener_config={
                    "match_conditions": {
                        "entity_id": "person.alex",  # Missing full name
                    }
                },
            )

            # Parse result
            data = json.loads(result)
            assert data["success"] is False
            assert data["message"] == "Validation failed"
            assert "validation_errors" in data

            # Check the validation error
            error = data["validation_errors"][0]
            assert error["field"] == "entity_id"
            assert "not found in Home Assistant" in error["error"]
            assert error["suggestion"] == "Did you mean 'person.alex_smith'?"
            assert "person.alex_smith" in error["similar_values"]

    @pytest.mark.asyncio
    async def test_create_listener_with_valid_entity_succeeds(
        self, db_engine: AsyncEngine, event_processor_with_ha: EventProcessor
    ) -> None:
        """Test that creating a listener with valid entity succeeds."""
        async with DatabaseContext(engine=db_engine) as db_ctx:
            # Create execution context
            exec_context = ToolExecutionContext(
                interface_type="telegram",
                conversation_id="test_validation",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_ctx,
            )

            # Set event sources for validation
            exec_context.event_sources = event_processor_with_ha.sources

            # Create listener with valid entity
            result = await create_event_listener_tool(
                exec_context=exec_context,
                name="Valid Entity Test",
                source="home_assistant",
                listener_config={
                    "match_conditions": {
                        "entity_id": "person.alex_smith",  # Valid entity
                    }
                },
            )

            # Parse result
            data = json.loads(result)
            assert data["success"] is True
            assert "listener_id" in data
            assert data["listener_id"] > 0

    @pytest.mark.asyncio
    async def test_create_listener_with_non_string_entity(
        self, db_engine: AsyncEngine, event_processor_with_ha: EventProcessor
    ) -> None:
        """Test that creating a listener with non-string entity ID fails."""
        async with DatabaseContext(engine=db_engine) as db_ctx:
            # Create execution context
            exec_context = ToolExecutionContext(
                interface_type="telegram",
                conversation_id="test_validation",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_ctx,
            )

            # Set event sources for validation
            exec_context.event_sources = event_processor_with_ha.sources

            # Try to create listener with numeric entity ID
            result = await create_event_listener_tool(
                exec_context=exec_context,
                name="Non-string Entity Test",
                source="home_assistant",
                listener_config={
                    "match_conditions": {
                        "entity_id": 12345,  # Not a string
                    }
                },
            )

            # Parse result
            data = json.loads(result)
            assert data["success"] is False
            assert data["message"] == "Validation failed"
            assert "validation_errors" in data

            # Check the validation error
            error = data["validation_errors"][0]
            assert error["field"] == "entity_id"
            assert "must be a string" in error["error"]
            assert "got int" in error["error"]

    @pytest.mark.asyncio
    async def test_create_listener_shows_similar_entities(
        self, db_engine: AsyncEngine, event_processor_with_ha: EventProcessor
    ) -> None:
        """Test that validation shows similar entities when entity not found."""
        async with DatabaseContext(engine=db_engine) as db_ctx:
            # Create execution context
            exec_context = ToolExecutionContext(
                interface_type="telegram",
                conversation_id="test_validation",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_ctx,
            )

            # Set event sources for validation
            exec_context.event_sources = event_processor_with_ha.sources

            # Try to create listener with non-existent light
            result = await create_event_listener_tool(
                exec_context=exec_context,
                name="Similar Entities Test",
                source="home_assistant",
                listener_config={
                    "match_conditions": {
                        "entity_id": "light.bedroom",  # Doesn't exist
                    }
                },
            )

            # Parse result
            data = json.loads(result)
            assert data["success"] is False
            assert data["message"] == "Validation failed"
            assert "validation_errors" in data

            # Check the validation error
            error = data["validation_errors"][0]
            assert error["field"] == "entity_id"
            assert "not found in Home Assistant" in error["error"]
            # Should show similar light entity
            assert "light.living_room" in error["similar_values"]

    @pytest.mark.asyncio
    async def test_create_listener_api_error_becomes_warning(
        self, db_engine: AsyncEngine, mock_ha_client: Mock
    ) -> None:
        """Test that API errors during validation become warnings, not hard failures."""
        # Make the HA client raise an error
        mock_ha_client.get_states.side_effect = Exception("API connection failed")

        # Create processor with failing client
        ha_source = HomeAssistantSource(mock_ha_client)
        processor = EventProcessor(sources={"home_assistant": ha_source})

        async with DatabaseContext(engine=db_engine) as db_ctx:
            # Create execution context
            exec_context = ToolExecutionContext(
                interface_type="telegram",
                conversation_id="test_validation",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_ctx,
            )

            # Set event sources for validation
            exec_context.event_sources = processor.sources

            # Create listener - should succeed despite API error
            result = await create_event_listener_tool(
                exec_context=exec_context,
                name="API Error Test",
                source="home_assistant",
                listener_config={
                    "match_conditions": {
                        "entity_id": "person.alex_smith",
                    }
                },
            )

            # Parse result - should succeed with warning
            data = json.loads(result)
            assert data["success"] is True
            assert "listener_id" in data
            # The warning might be logged but not necessarily in the response
