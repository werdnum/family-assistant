"""Test that LocalToolsProvider can retrieve calendar_config from ProcessingService."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.infrastructure import LocalToolsProvider
from family_assistant.tools.types import ToolExecutionContext


@pytest.mark.asyncio
async def test_calendar_config_from_processing_service() -> None:
    """Test that calendar_config can be retrieved from ProcessingService when not set on provider."""

    # Create a mock ProcessingService with calendar_config
    mock_processing_service = MagicMock()
    mock_processing_service.calendar_config = {
        "caldav": {
            "username": "test_user",
            "password": "test_pass",
            "calendar_urls": ["https://example.com/cal"],
        }
    }

    # Create a mock calendar tool function
    async def mock_calendar_tool(calendar_config: dict[str, Any], summary: str) -> str:
        """Mock calendar tool that requires calendar_config."""
        caldav_config = calendar_config.get("caldav", {})
        username = caldav_config.get("username", "NO_USER")
        return f"Calendar user: {username}, Event: {summary}"

    # Create LocalToolsProvider WITHOUT calendar_config (simulating root provider)
    provider = LocalToolsProvider(
        definitions=[
            {
                "type": "function",
                "function": {
                    "name": "mock_calendar_tool",
                    "description": "Test calendar tool",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "summary": {
                                "type": "string",
                                "description": "Event summary",
                            }
                        },
                        "required": ["summary"],
                    },
                },
            }
        ],
        implementations={"mock_calendar_tool": mock_calendar_tool},
        embedding_generator=None,
        calendar_config=None,  # This is the key test case
    )

    # Create execution context with ProcessingService
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="Test User",
        turn_id=None,
        db_context=MagicMock(spec=DatabaseContext),
        processing_service=mock_processing_service,  # Has calendar_config
        timezone_str="UTC",
    )

    # Execute the tool - should get calendar_config from ProcessingService
    result = await provider.execute_tool(
        name="mock_calendar_tool", arguments={"summary": "Test Event"}, context=context
    )

    # Verify the calendar_config was successfully retrieved and used
    assert "Calendar user: test_user" in result
    assert "Event: Test Event" in result
    assert "Error" not in result


@pytest.mark.asyncio
async def test_calendar_config_fallback_to_instance() -> None:
    """Test that instance calendar_config is preferred over ProcessingService."""

    # Create provider WITH calendar_config
    instance_config = {
        "caldav": {
            "username": "instance_user",
            "password": "instance_pass",
            "calendar_urls": ["https://instance.com/cal"],
        }
    }

    # Create a mock ProcessingService with different calendar_config
    mock_processing_service = MagicMock()
    mock_processing_service.calendar_config = {
        "caldav": {
            "username": "service_user",
            "password": "service_pass",
            "calendar_urls": ["https://service.com/cal"],
        }
    }

    # Create a mock calendar tool function
    async def mock_calendar_tool(calendar_config: dict[str, Any], summary: str) -> str:
        """Mock calendar tool that requires calendar_config."""
        caldav_config = calendar_config.get("caldav", {})
        username = caldav_config.get("username", "NO_USER")
        return f"Calendar user: {username}, Event: {summary}"

    provider = LocalToolsProvider(
        definitions=[
            {
                "type": "function",
                "function": {
                    "name": "mock_calendar_tool",
                    "description": "Test calendar tool",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "summary": {
                                "type": "string",
                                "description": "Event summary",
                            }
                        },
                        "required": ["summary"],
                    },
                },
            }
        ],
        implementations={"mock_calendar_tool": mock_calendar_tool},
        embedding_generator=None,
        calendar_config=instance_config,  # Has its own config
    )

    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="Test User",
        turn_id=None,
        db_context=MagicMock(spec=DatabaseContext),
        processing_service=mock_processing_service,
        timezone_str="UTC",
    )

    result = await provider.execute_tool(
        name="mock_calendar_tool", arguments={"summary": "Test Event"}, context=context
    )

    # Should use instance config, not ProcessingService config
    assert "Calendar user: instance_user" in result
    assert "service_user" not in result


@pytest.mark.asyncio
async def test_calendar_config_missing_error() -> None:
    """Test error when calendar_config is not available anywhere."""

    # Create a mock calendar tool function
    async def mock_calendar_tool(calendar_config: dict[str, Any], summary: str) -> str:
        """Mock calendar tool that requires calendar_config."""
        return "Should not reach here"

    provider = LocalToolsProvider(
        definitions=[
            {
                "type": "function",
                "function": {
                    "name": "mock_calendar_tool",
                    "description": "Test calendar tool",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "summary": {
                                "type": "string",
                                "description": "Event summary",
                            }
                        },
                        "required": ["summary"],
                    },
                },
            }
        ],
        implementations={"mock_calendar_tool": mock_calendar_tool},
        embedding_generator=None,
        calendar_config=None,  # No config
    )

    # Context without ProcessingService
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="Test User",
        turn_id=None,
        db_context=MagicMock(spec=DatabaseContext),
        processing_service=None,  # No ProcessingService
        timezone_str="UTC",
    )

    result = await provider.execute_tool(
        name="mock_calendar_tool", arguments={"summary": "Test Event"}, context=context
    )

    # Should return error message
    assert "Error:" in result
    assert "calendar_config is missing" in result
