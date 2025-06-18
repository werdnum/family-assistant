"""
Tests for the Starlark tools API bridge.
"""

from typing import Any

import pytest

from family_assistant.scripting.engine import StarlarkEngine
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.types import ToolExecutionContext


class MockToolsProvider:
    """Mock tools provider for testing."""

    async def get_tool_definitions(self) -> list[dict[str, Any]]:
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
        self, name: str, arguments: dict[str, Any], context: ToolExecutionContext
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

    async def close(self) -> None:
        """No cleanup needed for mock."""
        pass


@pytest.mark.asyncio
async def test_tools_api_list(test_db_engine: Any) -> None:
    """Test listing tools from Starlark."""
    # Create mock tools provider
    tools_provider = MockToolsProvider()

    # Create execution context
    async with DatabaseContext() as db:
        context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-123",
            user_name="Test User",
            turn_id="turn-1",
            db_context=db,
        )

        # Create engine
        engine = StarlarkEngine(tools_provider=tools_provider)

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
async def test_tools_api_get(test_db_engine: Any) -> None:
    """Test getting a specific tool from Starlark."""
    tools_provider = MockToolsProvider()

    async with DatabaseContext() as db:
        context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-123",
            user_name="Test User",
            turn_id="turn-1",
            db_context=db,
        )

        engine = StarlarkEngine(tools_provider=tools_provider)

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
async def test_tools_api_execute(test_db_engine: Any) -> None:
    """Test executing tools from Starlark."""
    tools_provider = MockToolsProvider()

    async with DatabaseContext() as db:
        context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-123",
            user_name="Test User",
            turn_id="turn-1",
            db_context=db,
        )

        engine = StarlarkEngine(tools_provider=tools_provider)

        # Test executing echo tool
        script = """
result = tools_execute("echo", message="Hello, Starlark!")
result
"""

        result = await engine.evaluate_async(script, execution_context=context)
        assert result == "Echo: Hello, Starlark!"

        # Test executing add_numbers tool
        script2 = """
result = tools_execute("add_numbers", a=5, b=3)
result
"""

        result2 = await engine.evaluate_async(script2, execution_context=context)
        assert result2 == "Result: 8"


@pytest.mark.asyncio
async def test_tools_api_execute_json(test_db_engine: Any) -> None:
    """Test executing tools with JSON arguments from Starlark."""
    tools_provider = MockToolsProvider()

    async with DatabaseContext() as db:
        context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-123",
            user_name="Test User",
            turn_id="turn-1",
            db_context=db,
        )

        engine = StarlarkEngine(tools_provider=tools_provider)

        # Test executing with JSON arguments
        script = """
args_json = '{"message": "JSON test"}'
result = tools_execute_json("echo", args_json)
result
"""

        result = await engine.evaluate_async(script, execution_context=context)
        assert result == "Echo: JSON test"


@pytest.mark.asyncio
async def test_tools_api_not_available_without_context(test_db_engine: Any) -> None:
    """Test that tools API is not available without execution context."""
    tools_provider = MockToolsProvider()
    engine = StarlarkEngine(tools_provider=tools_provider)

    # Script that tries to use tools - should fail without context
    script = """
# This should work without context (no tools available)
result = "no tools"
result
"""

    # Execute without context - should work
    result = await engine.evaluate_async(script)
    assert result == "no tools"

    # Now try to use a tool function that shouldn't exist
    script2 = """
# This should fail because tools_list is not defined
tools_list()
"""

    # This should raise an error
    with pytest.raises(Exception) as exc_info:
        await engine.evaluate_async(script2)
    assert "not found" in str(exc_info.value) or "NameError" in str(exc_info.value)


@pytest.mark.asyncio
async def test_tools_api_invalid_tool(test_db_engine: Any) -> None:
    """Test that executing an invalid tool raises an error."""
    tools_provider = MockToolsProvider()

    async with DatabaseContext() as db:
        context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-123",
            user_name="Test User",
            turn_id="turn-1",
            db_context=db,
        )

        engine = StarlarkEngine(tools_provider=tools_provider)

        # Test executing non-existent tool - should raise an error
        script = """
# This should fail because the tool doesn't exist
tools_execute("nonexistent", arg="value")
"""

        # Since Starlark doesn't have try/except, this will raise an exception
        with pytest.raises(Exception) as exc_info:
            await engine.evaluate_async(script, execution_context=context)

        # Check that the error mentions the unknown tool
        assert "Unknown tool" in str(exc_info.value)
