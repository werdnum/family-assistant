"""
Unit tests for event listener tool validation integration.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.events.sources import BaseEventSource
from family_assistant.events.validation import ValidationError, ValidationResult
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools import events
from family_assistant.tools.event_listeners import create_event_listener_tool
from family_assistant.tools.types import ToolExecutionContext

# Access test_event_listener_tool through the module to avoid modifying it
# This prevents potential issues with parallel test execution


class TestCreateEventListenerValidation:
    """Test validation in create_event_listener_tool."""

    @pytest.mark.asyncio
    async def test_create_listener_with_validation_errors(self) -> None:
        """Test creating a listener with validation errors."""
        # Mock the execution context
        exec_context = MagicMock(spec=ToolExecutionContext)
        exec_context.db_context = AsyncMock()  # Ensure db_context is async-compatible

        # Mock event processor and source
        mock_source = MagicMock(spec=BaseEventSource)
        mock_source.source_id = "home_assistant"
        mock_source.validate_match_conditions = AsyncMock(
            return_value=ValidationResult(
                valid=False,
                errors=[
                    ValidationError(
                        field="entity_id",
                        value="person.invalid",
                        error="Entity does not exist",
                        suggestion="Did you mean 'person.valid'?",
                        similar_values=["person.valid", "person.andrew"],
                    ),
                    ValidationError(
                        field="new_state.state",
                        value="Home",
                        error="Invalid state for person entity",
                        suggestion="Use 'home' (lowercase)",
                    ),
                ],
                warnings=["Some warning"],
            )
        )

        # Setup the event sources
        exec_context.event_sources = {"home_assistant": mock_source}

        # Test the tool
        result = await create_event_listener_tool(
            exec_context=exec_context,
            name="test_listener",
            source="home_assistant",
            listener_config={
                "match_conditions": {
                    "entity_id": "person.invalid",
                    "new_state.state": "Home",
                }
            },
        )

        # Parse result
        data = json.loads(result)

        # Verify validation was called
        mock_source.validate_match_conditions.assert_called_once()

        # Check result
        assert data["success"] is False
        assert data["message"] == "Validation failed"
        assert len(data["validation_errors"]) == 2
        assert data["validation_errors"][0]["field"] == "entity_id"
        assert (
            data["validation_errors"][0]["suggestion"] == "Did you mean 'person.valid'?"
        )
        assert data["validation_errors"][1]["field"] == "new_state.state"
        assert data["warnings"] == ["Some warning"]

    @pytest.mark.asyncio
    async def test_create_listener_with_validation_warnings(self) -> None:
        """Test creating a listener with validation warnings only."""
        # Mock the execution context
        exec_context = MagicMock(spec=ToolExecutionContext)
        exec_context.db_context = AsyncMock()  # Ensure db_context is async-compatible
        exec_context.conversation_id = "test_conv"
        exec_context.interface_type = "test"

        # Mock event processor and source
        mock_source = MagicMock(spec=BaseEventSource)
        mock_source.source_id = "home_assistant"
        mock_source.validate_match_conditions = AsyncMock(
            return_value=ValidationResult(
                valid=True, warnings=["Cannot validate state without entity_id"]
            )
        )

        # Setup the event sources
        exec_context.event_sources = {"home_assistant": mock_source}

        # Mock the database creation
        with patch(
            "family_assistant.tools.event_listeners.create_event_listener",
            return_value=123,
        ):
            result = await create_event_listener_tool(
                exec_context=exec_context,
                name="test_listener",
                source="home_assistant",
                listener_config={"match_conditions": {"state": "on"}},
            )

        # Parse result
        data = json.loads(result)

        # Check result - should succeed with warning logged
        assert data["success"] is True
        assert data["listener_id"] == 123

    @pytest.mark.asyncio
    async def test_create_listener_no_validation_support(self) -> None:
        """Test creating a listener when source doesn't support validation."""
        # Mock the execution context
        exec_context = MagicMock(spec=ToolExecutionContext)
        exec_context.db_context = AsyncMock()
        exec_context.conversation_id = "test_conv"
        exec_context.interface_type = "test"

        # Mock event processor with source that doesn't have validate_match_conditions
        # Use object() as spec to ensure no methods are available
        mock_source = MagicMock(spec=object())

        # Setup the event sources
        exec_context.event_sources = {"home_assistant": mock_source}

        # Mock the database creation
        with patch(
            "family_assistant.tools.event_listeners.create_event_listener",
            return_value=124,
        ):
            result = await create_event_listener_tool(
                exec_context=exec_context,
                name="test_listener",
                source="home_assistant",
                listener_config={"match_conditions": {"entity_id": "anything"}},
            )

        # Parse result
        data = json.loads(result)

        # Should succeed without validation
        assert data["success"] is True
        assert data["listener_id"] == 124


class TestEventListenerTestValidation:
    """Test validation in test_event_listener_tool."""

    @pytest.mark.asyncio
    async def test_test_listener_with_validation_errors(
        self, db_engine: AsyncEngine
    ) -> None:
        """Test testing a listener that shows validation errors in analysis."""
        # Create execution context with real database
        async with DatabaseContext(engine=db_engine) as db_context:
            exec_context = MagicMock(spec=ToolExecutionContext)
            exec_context.db_context = db_context

            # Mock event processor and source
            mock_source = MagicMock(spec=BaseEventSource)
            mock_source.source_id = "home_assistant"
            mock_source.validate_match_conditions = AsyncMock(
                return_value=ValidationResult(
                    valid=False,
                    errors=[
                        ValidationError(
                            field="entity_id",
                            value="invalid.entity",
                            error="Invalid entity ID format",
                            suggestion="Format: domain.object_id",
                        )
                    ],
                )
            )

            # Setup the event sources
            exec_context.event_sources = {"home_assistant": mock_source}

            # Call the function directly
            result = await events.test_event_listener_tool(
                exec_context=exec_context,
                source="home_assistant",
                match_conditions={"entity_id": "invalid.entity"},
                hours=1,
            )

            # Parse result
            data = json.loads(result)

            # Check that validation errors appear in analysis
            assert data["matched_events"] == []
            assert data["total_tested"] == 0
            assert "analysis" not in data  # No analysis when no events tested
