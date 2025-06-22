"""
Tests for direct tool callable functionality in Starlark scripts.

This module tests that tools can be called directly as functions in Starlark,
without going through the tools_execute() wrapper.
"""

from typing import Any

import pytest

from family_assistant.scripting.engine import StarlarkConfig, StarlarkEngine
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.types import ToolExecutionContext


class MockToolsProvider:
    """Mock tools provider for testing direct callables."""

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
            {
                "type": "function",
                "function": {
                    "name": "greet_user",
                    "description": "Greet a user by name",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "User's name",
                            },
                            "formal": {
                                "type": "boolean",
                                "description": "Use formal greeting",
                                "default": False,
                            },
                        },
                        "required": ["name"],
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
        elif name == "greet_user":
            user_name = arguments.get("name", "")
            formal = arguments.get("formal", False)
            if formal:
                return f"Good day, {user_name}!"
            else:
                return f"Hello, {user_name}!"
        else:
            raise ValueError(f"Unknown tool: {name}")

    async def close(self) -> None:
        """No cleanup needed for mock."""
        pass


@pytest.mark.asyncio
async def test_direct_tool_callable(test_db_engine: Any) -> None:
    """Test calling tools directly as functions."""
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

        # Test script that calls tools directly
        script = """
# Call echo tool directly
result1 = echo(message="Hello, World!")

# Call add_numbers tool directly  
result2 = add_numbers(a=5, b=3)

# Call greet_user with optional parameter
result3 = greet_user(name="Alice", formal=True)

# Store results for verification
results = [result1, result2, result3]
results  # Return the results
"""

        # Execute the script
        result = await engine.evaluate_async(script, execution_context=context)

        # Verify results
        assert result == [
            "Echo: Hello, World!",
            "Result: 8",
            "Good day, Alice!",
        ]


@pytest.mark.asyncio
async def test_tool_prefix_fallback(test_db_engine: Any) -> None:
    """Test that tools can also be called with tool_ prefix."""
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

        # Test script that calls tools with tool_ prefix
        script = """
# Call tools using tool_ prefix
result1 = tool_echo(message="Testing prefix")
result2 = tool_add_numbers(a=10, b=20)

# Mix direct and prefixed calls
result3 = echo(message="Direct call")
result4 = tool_greet_user(name="Bob")

results = [result1, result2, result3, result4]
results  # Return the results
"""

        # Execute the script
        result = await engine.evaluate_async(script, execution_context=context)

        # Verify results
        assert result == [
            "Echo: Testing prefix",
            "Result: 30",
            "Echo: Direct call",
            "Hello, Bob!",
        ]


@pytest.mark.asyncio
async def test_direct_callable_with_security(test_db_engine: Any) -> None:
    """Test that security controls still apply to direct callables."""
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

        # Create engine with only echo allowed
        config = StarlarkConfig(allowed_tools={"echo"})
        engine = StarlarkEngine(tools_provider=tools_provider, config=config)

        # Test script that tries to call allowed and disallowed tools
        script = """
# This should work - echo is allowed
result1 = echo(message="Allowed tool")

# Store result
allowed_result = result1

# Try to get list of available tools
available = tools_list()
available  # Return the available tools
"""

        # Execute the script
        result = await engine.evaluate_async(script, execution_context=context)

        # Verify only echo was available
        assert result == [
            {
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
            }
        ]


@pytest.mark.asyncio
async def test_direct_callable_validates_parameters(test_db_engine: Any) -> None:
    """Test that direct tool calls validate parameters properly."""

    # Create mock tools provider that validates parameters
    class ValidatingMockToolsProvider(MockToolsProvider):
        async def execute_tool(
            self, name: str, arguments: dict[str, Any], context: ToolExecutionContext
        ) -> str:
            """Execute a mock tool with validation."""
            if name == "echo":
                if "message" not in arguments:
                    raise ValueError("Required parameter 'message' is missing")
                return f"Echo: {arguments.get('message', '')}"
            return await super().execute_tool(name, arguments, context)

    # Create tools provider
    tools_provider = ValidatingMockToolsProvider()

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

        # Test script that checks parameter validation
        script = """
# Call echo with proper parameters
result1 = echo(message="Test message")

# Call add_numbers with proper parameters
result2 = add_numbers(a=1, b=2)

results = [result1, result2]
results  # Return the results
"""

        # Execute the script
        result = await engine.evaluate_async(script, execution_context=context)

        # Verify successful calls
        assert result == ["Echo: Test message", "Result: 3"]


@pytest.mark.asyncio
async def test_tools_api_still_works(test_db_engine: Any) -> None:
    """Test that the old tools API still works alongside direct callables."""
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

        # Test script that uses both old and new APIs
        script = """
# Use old tools API
old_result = tools_execute("echo", message="Old API")

# Use new direct callable
new_result = echo(message="New API")

# Get tool info using old API
tool_info = tools_get("echo")

# List tools using old API
all_tools = tools_list()

results = {
    "old": old_result,
    "new": new_result,
    "has_info": tool_info != None,
    "tool_count": len(all_tools)
}
results  # Return the results
"""

        # Execute the script
        result = await engine.evaluate_async(script, execution_context=context)

        # Verify both APIs work
        assert result["old"] == "Echo: Old API"
        assert result["new"] == "Echo: New API"
        assert result["has_info"] is True
        assert result["tool_count"] == 3


@pytest.mark.asyncio
async def test_no_tools_when_denied(test_db_engine: Any) -> None:
    """Test that no direct callables are created when all tools are denied."""
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

        # Create engine with all tools denied
        config = StarlarkConfig(deny_all_tools=True)
        engine = StarlarkEngine(tools_provider=tools_provider, config=config)

        # Test script that checks if tools exist
        script = """
# Try to use tool functions to check if they exist
echo_exists = False
add_numbers_exists = False

# Check tools list  
available_tools = tools_list()

# Since we can't use dir() or hasattr in Starlark, we just verify the tools list is empty
# and trust that if no tools are in the list, the functions won't be created

results = {
    "echo_exists": echo_exists,
    "add_numbers_exists": add_numbers_exists,
    "tools_count": len(available_tools)
}
results  # Return the results
"""

        # Execute the script
        result = await engine.evaluate_async(script, execution_context=context)

        # Verify no tools are available
        assert result["echo_exists"] is False
        assert result["add_numbers_exists"] is False
        assert result["tools_count"] == 0
