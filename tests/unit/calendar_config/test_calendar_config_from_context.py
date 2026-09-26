"""Calendar tools get their calendar_config from the execution context."""

from typing import cast
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from family_assistant.storage.database import Database
from family_assistant.tools.infrastructure import LocalToolsProvider
from family_assistant.tools.types import (
    CalendarConfig,
    ToolExecutionContext,
    ToolResult,
)


@pytest.mark.asyncio
async def test_calendar_config_from_context() -> None:
    """The context's calendar_config is injected into a calendar tool."""

    test_calendar_config = cast(
        "CalendarConfig",
        {
            "caldav": {
                "username": "test_user",
                "password": "test_pass",
                "calendar_urls": ["https://example.com/cal"],
            }
        },
    )

    # Create a mock calendar tool function
    # ast-grep-ignore: no-dict-any - CalendarConfig is a TypedDict with dynamic nested fields
    async def mock_calendar_tool(calendar_config: CalendarConfig, summary: str) -> str:
        """Mock calendar tool that requires calendar_config."""
        caldav_config = calendar_config.get("caldav") or {}
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
    )

    # Create execution context
    mock_db_context = MagicMock(spec=Database)
    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="TestUser",
        turn_id="test_turn",
        db_context=mock_db_context,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        chat_interface=None,
        timezone=ZoneInfo("UTC"),
        camera_backend=None,
        credential_resolvers=None,
        api_backend=None,
        calendar_config=test_calendar_config,
    )

    # Execute the tool
    result = await provider.execute_tool(
        name="mock_calendar_tool",
        arguments={"summary": "Test Meeting"},
        context=exec_context,
    )

    assert result == "Calendar user: test_user, Event: Test Meeting"


@pytest.mark.asyncio
async def test_calendar_tool_without_config() -> None:
    """Test that calendar tool fails gracefully when calendar_config is not available."""

    # Create a mock calendar tool function
    # ast-grep-ignore: no-dict-any - CalendarConfig is a TypedDict with dynamic nested fields
    async def mock_calendar_tool(calendar_config: CalendarConfig, summary: str) -> str:
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
    )

    # Create execution context
    mock_db_context = MagicMock(spec=Database)
    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="TestUser",
        turn_id="test_turn",
        db_context=mock_db_context,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        chat_interface=None,
        timezone=ZoneInfo("UTC"),
        camera_backend=None,
        credential_resolvers=None,
        api_backend=None,
    )

    # Execute the tool - should return error
    result = await provider.execute_tool(
        name="mock_calendar_tool",
        arguments={"summary": "Test Meeting"},
        context=exec_context,
    )

    # Verify the tool returned an error

    result_text = result.get_text() if isinstance(result, ToolResult) else str(result)
    assert (
        "Error: Tool 'mock_calendar_tool' cannot be executed because the calendar_config is missing."
        in result_text
    )
