"""
Tests for the scripting tools API bridge.
"""

from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.scripting.errors import ScriptExecutionError
from family_assistant.scripting.monty_engine import MontyEngine
from family_assistant.storage.database import Database
from family_assistant.tools.infrastructure import (
    CompositeToolsProvider,
    LocalToolsProvider,
)
from family_assistant.tools.types import ToolDefinition, ToolExecutionContext


class MockToolsProvider:
    """Mock tools provider for testing."""

    async def get_tool_definitions(self) -> list[ToolDefinition]:
        """Return mock tool definitions."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "description": "Echo back the input message",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "message": {
                                "type": "string",
                                "description": "Message to echo",
                            }
                        },
                        "required": ["message"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "add_numbers",
                    "description": "Add two numbers together",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "a": {
                                "type": "number",
                                "description": "First number",
                            },
                            "b": {
                                "type": "number",
                                "description": "Second number",
                            },
                        },
                        "required": ["a", "b"],
                    },
                },
            },
        ]

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - tool arguments from external LLM tool call
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str:
        """Execute a mock tool."""
        if name == "echo":
            return f"Echo: {arguments.get('message', '')}"
        elif name == "add_numbers":
            a = arguments.get("a", 0)
            b = arguments.get("b", 0)
            return f"Result: {a + b}"
        else:
            raise ValueError(f"Unknown tool: {name}")

    # ast-grep-ignore: no-dict-any - tool definitions match external OpenAI JSON schema
    def get_raw_tool_definitions(self) -> list[dict[str, Any]]:
        """Return raw tool definitions (same as translated for mock)."""
        # For the mock, raw and translated definitions are the same
        # Return the definitions synchronously to avoid event loop issues
        return [
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "description": "Echo back the input message",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "message": {
                                "type": "string",
                                "description": "Message to echo",
                            }
                        },
                        "required": ["message"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "add_numbers",
                    "description": "Add two numbers together",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "a": {
                                "type": "number",
                                "description": "First number",
                            },
                            "b": {
                                "type": "number",
                                "description": "Second number",
                            },
                        },
                        "required": ["a", "b"],
                    },
                },
            },
        ]

    async def close(self) -> None:
        """No cleanup needed for mock."""


@pytest.mark.asyncio
async def test_tools_api_list(db_engine: AsyncEngine) -> None:
    """Test listing tools from script."""
    # Create mock tools provider
    tools_provider = MockToolsProvider()

    # Create execution context
    db = Database(engine=db_engine)
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-123",
        user_name="Test User",
        turn_id="turn-1",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    # Create engine
    engine = MontyEngine(
        tools_provider=tools_provider, default_timezone=ZoneInfo("Australia/Sydney")
    )

    # Test script that lists tools
    script = """
tools_list = tools_list()
tool_names = [tool["name"] for tool in tools_list]
tool_names
"""

    # Execute script
    result = await engine.evaluate_async(script, execution_context=context)

    # Verify we got the expected tools
    assert result == ["echo", "add_numbers"]


@pytest.mark.asyncio
async def test_tools_api_get(db_engine: AsyncEngine) -> None:
    """Test getting a specific tool from script."""
    tools_provider = MockToolsProvider()

    db = Database(engine=db_engine)
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-123",
        user_name="Test User",
        turn_id="turn-1",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    engine = MontyEngine(
        tools_provider=tools_provider, default_timezone=ZoneInfo("Australia/Sydney")
    )

    # Test script that gets a specific tool
    script = """
echo_tool = tools_get("echo")
echo_tool["name"] if echo_tool else None
"""

    result = await engine.evaluate_async(script, execution_context=context)
    assert result == "echo"

    # Test getting non-existent tool
    script2 = """
fake_tool = tools_get("nonexistent")
fake_tool
"""

    result2 = await engine.evaluate_async(script2, execution_context=context)
    assert result2 is None


@pytest.mark.asyncio
async def test_tools_api_execute(db_engine: AsyncEngine) -> None:
    """Test executing tools from script."""
    tools_provider = MockToolsProvider()

    db = Database(engine=db_engine)
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-123",
        user_name="Test User",
        turn_id="turn-1",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    engine = MontyEngine(
        tools_provider=tools_provider, default_timezone=ZoneInfo("Australia/Sydney")
    )

    # Test executing echo tool
    script = """
result = tools_execute("echo", message="Hello, Script!")
result
"""

    result = await engine.evaluate_async(script, execution_context=context)
    assert result == "Echo: Hello, Script!"

    # Test executing add_numbers tool
    script2 = """
result = tools_execute("add_numbers", a=5, b=3)
result
"""

    result2 = await engine.evaluate_async(script2, execution_context=context)
    assert result2 == "Result: 8"


@pytest.mark.asyncio
async def test_tools_api_execute_json(db_engine: AsyncEngine) -> None:
    """Test executing tools with JSON arguments from script."""
    tools_provider = MockToolsProvider()

    db = Database(engine=db_engine)
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-123",
        user_name="Test User",
        turn_id="turn-1",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    engine = MontyEngine(
        tools_provider=tools_provider, default_timezone=ZoneInfo("Australia/Sydney")
    )

    # Test executing with JSON arguments
    script = """
args_json = '{"message": "JSON test"}'
result = tools_execute_json("echo", args_json)
result
"""

    result = await engine.evaluate_async(script, execution_context=context)
    assert result == "Echo: JSON test"


@pytest.mark.asyncio
async def test_tools_api_not_available_without_context(db_engine: AsyncEngine) -> None:
    """Test that tools API is not available without execution context."""
    tools_provider = MockToolsProvider()
    engine = MontyEngine(
        tools_provider=tools_provider, default_timezone=ZoneInfo("Australia/Sydney")
    )

    with pytest.raises(ScriptExecutionError, match="name 'tools_list' is not defined"):
        await engine.evaluate_async("tools_list()")


async def _echo(message: str) -> str:
    return f"Echo: {message}"


@pytest.mark.asyncio
async def test_tools_api_invalid_tool(db_engine: AsyncEngine) -> None:
    """Test that executing a tool no provider registers fails the script."""
    echo_definition: ToolDefinition = {
        "type": "function",
        "function": {
            "name": "echo",
            "description": "Echo back the input message",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        },
    }
    tools_provider = CompositeToolsProvider(
        providers=[
            LocalToolsProvider(
                definitions=[echo_definition], implementations={"echo": _echo}
            )
        ]
    )

    db = Database(engine=db_engine)
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-123",
        user_name="Test User",
        turn_id="turn-1",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    engine = MontyEngine(
        tools_provider=tools_provider, default_timezone=ZoneInfo("Australia/Sydney")
    )

    script = """
tools_execute("nonexistent", arg="value")
"""

    with pytest.raises(ScriptExecutionError, match="Tool 'nonexistent' not found"):
        await engine.evaluate_async(script, execution_context=context)
