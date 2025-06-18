"""Test that LocalToolsProvider handles calendar_config properly."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.infrastructure import LocalToolsProvider
from family_assistant.tools.types import ToolExecutionContext


@pytest.mark.asyncio
async def test_calendar_config_from_provider() -> None:
    """Test that calendar_config is used from provider when set."""

    test_calendar_config = {
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

    # Create LocalToolsProvider WITH calendar_config
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
        calendar_config=test_calendar_config,
    )

    # Create execution context
    mock_db_context = MagicMock(spec=DatabaseContext)
    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="TestUser",
        turn_id="test_turn",
        db_context=mock_db_context,
        chat_interface=None,
        timezone_str="UTC",
    )

    # Execute the tool
    result = await provider.execute_tool(
        name="mock_calendar_tool",
        arguments={"summary": "Test Meeting"},
        context=exec_context,
    )

    # Verify the tool received the calendar_config from the provider
    assert result == "Calendar user: test_user, Event: Test Meeting"


@pytest.mark.asyncio
async def test_calendar_tool_without_config() -> None:
    """Test that calendar tool fails gracefully when calendar_config is not available."""

    # Create a mock calendar tool function
    async def mock_calendar_tool(calendar_config: dict[str, Any], summary: str) -> str:
        """Mock calendar tool that requires calendar_config."""
        return "Should not reach here"

    # Create LocalToolsProvider WITHOUT calendar_config
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
        calendar_config=None,
    )

    # Create execution context
    mock_db_context = MagicMock(spec=DatabaseContext)
    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="TestUser",
        turn_id="test_turn",
        db_context=mock_db_context,
        chat_interface=None,
        timezone_str="UTC",
    )

    # Execute the tool - should return error
    result = await provider.execute_tool(
        name="mock_calendar_tool",
        arguments={"summary": "Test Meeting"},
        context=exec_context,
    )

    # Verify the tool returned an error
    assert (
        "Error: Tool 'mock_calendar_tool' cannot be executed because the calendar_config is missing."
        in result
    )


@pytest.mark.asyncio
async def test_calendar_config_preference() -> None:
    """Test that instance calendar_config is used when available."""

    instance_calendar_config = {
        "caldav": {
            "username": "instance_user",
            "password": "instance_pass",
            "calendar_urls": ["https://instance.example.com/cal"],
        }
    }

    # Create a mock calendar tool function
    async def mock_calendar_tool(calendar_config: dict[str, Any], summary: str) -> str:
        """Mock calendar tool that requires calendar_config."""
        caldav_config = calendar_config.get("caldav", {})
        username = caldav_config.get("username", "NO_USER")
        return f"Calendar user: {username}, Event: {summary}"

    # Create LocalToolsProvider with calendar_config
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
        calendar_config=instance_calendar_config,
    )

    # Create execution context
    mock_db_context = MagicMock(spec=DatabaseContext)
    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="TestUser",
        turn_id="test_turn",
        db_context=mock_db_context,
        chat_interface=None,
        timezone_str="UTC",
    )

    # Execute the tool
    result = await provider.execute_tool(
        name="mock_calendar_tool",
        arguments={"summary": "Important Meeting"},
        context=exec_context,
    )

    # Should use instance config
    assert result == "Calendar user: instance_user, Event: Important Meeting"
