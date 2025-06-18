"""Tests for the execute_script tool."""

from typing import Any
from unittest.mock import Mock

import pytest

from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.execute_script import execute_script_tool
from family_assistant.tools.infrastructure import (
    CompositeToolsProvider,
    LocalToolsProvider,
)
from family_assistant.tools.types import ToolExecutionContext


@pytest.mark.asyncio
async def test_execute_script_without_tools_provider(test_db_engine: Any) -> None:
    """Test execute_script when no tools provider is available."""
    async with DatabaseContext() as db:
        # Create context without processing service
        ctx = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-conv",
            user_name="test",
            turn_id=None,
            db_context=db,
            processing_service=None,
        )

        # Simple script should work
        result = await execute_script_tool(ctx, 'print("Hello")')
        assert "Script executed successfully" in result or "Hello" in result

        # Script using tools should fail
        result = await execute_script_tool(ctx, "tools_list()")
        assert "Error:" in result
        assert "not found" in result


@pytest.mark.asyncio
async def test_execute_script_with_empty_tools_provider(test_db_engine: Any) -> None:
    """Test execute_script with an empty tools provider."""
    async with DatabaseContext() as db:
        # Create empty tools provider
        tools_provider = CompositeToolsProvider([])

        # Create mock processing service
        mock_service = Mock()
        mock_service.tools_provider = tools_provider

        ctx = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-conv",
            user_name="test",
            turn_id=None,
            db_context=db,
            processing_service=mock_service,
        )

        # Should be able to list tools (empty list)
        result = await execute_script_tool(
            ctx,
            """
tools = tools_list()
len(tools)
""",
        )
        assert "Script result: 0" in result


@pytest.mark.asyncio
async def test_execute_script_with_tools(test_db_engine: Any) -> None:
    """Test execute_script with actual tools available."""
    async with DatabaseContext() as db:
        # Create a simple echo tool
        async def echo_tool(message: str) -> str:
            return f"Echo: {message}"

        # Create tools provider
        tool_definitions = [
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "description": "Echo a message",
                    "parameters": {
                        "type": "object",
                        "properties": {"message": {"type": "string"}},
                        "required": ["message"],
                    },
                },
            }
        ]

        local_provider = LocalToolsProvider(
            definitions=tool_definitions, implementations={"echo": echo_tool}
        )

        tools_provider = CompositeToolsProvider([local_provider])

        # Create mock processing service
        mock_service = Mock()
        mock_service.tools_provider = tools_provider

        ctx = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-conv",
            user_name="test",
            turn_id=None,
            db_context=db,
            processing_service=mock_service,
        )

        # Test listing tools
        result = await execute_script_tool(
            ctx,
            """
tools = tools_list()
[tool["name"] for tool in tools]
""",
        )
        assert '"echo"' in result  # Just check that echo is in the result

        # Test executing tool
        result = await execute_script_tool(
            ctx,
            """
echo(message="Hello from Starlark!")
""",
        )
        assert "Echo: Hello from Starlark!" in result


@pytest.mark.asyncio
async def test_execute_script_syntax_error(test_db_engine: Any) -> None:
    """Test execute_script with syntax errors."""
    async with DatabaseContext() as db:
        ctx = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-conv",
            user_name="test",
            turn_id=None,
            db_context=db,
            processing_service=None,
        )

        # Invalid syntax
        result = await execute_script_tool(ctx, "if true")
        assert "Error:" in result
        assert "syntax" in result.lower() or "parse" in result.lower()


@pytest.mark.asyncio
@pytest.mark.slow  # This test takes 30 seconds to timeout
async def test_execute_script_timeout(test_db_engine: Any) -> None:
    """Test execute_script timeout handling."""
    async with DatabaseContext() as db:
        ctx = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-conv",
            user_name="test",
            turn_id=None,
            db_context=db,
            processing_service=None,
        )

        # Script with infinite loop (will timeout)
        # Note: Starlark requires loops to be inside functions
        result = await execute_script_tool(
            ctx,
            """
def infinite_loop():
    for i in range(1000000000):
        for j in range(1000000000):
            x = i * j
    return x

infinite_loop()
""",
        )
        assert "Error:" in result
        assert "timeout" in result.lower()


@pytest.mark.asyncio
async def test_execute_script_with_globals(test_db_engine: Any) -> None:
    """Test execute_script with global variables."""
    async with DatabaseContext() as db:
        ctx = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-conv",
            user_name="test",
            turn_id=None,
            db_context=db,
            processing_service=None,
        )

        # Pass globals
        result = await execute_script_tool(
            ctx,
            'user_name + " says " + str(count)',
            globals={"user_name": "Alice", "count": 42},
        )
        assert "Alice says 42" in result
