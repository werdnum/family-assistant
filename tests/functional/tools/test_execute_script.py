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
async def test_execute_script_without_tools_provider(db_engine: Any) -> None:
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
async def test_execute_script_with_empty_tools_provider(db_engine: Any) -> None:
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
async def test_execute_script_with_tools(db_engine: Any) -> None:
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
async def test_execute_script_syntax_error(db_engine: Any) -> None:
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
async def test_execute_script_with_globals(db_engine: Any) -> None:
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


@pytest.mark.asyncio
async def test_execute_script_with_wake_llm(db_engine: Any) -> None:
    """Test execute_script with wake_llm calls."""
    async with DatabaseContext() as db:
        ctx = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-conv",
            user_name="test",
            turn_id=None,
            db_context=db,
            processing_service=None,
        )

        # Test single wake_llm call
        result = await execute_script_tool(
            ctx,
            """
wake_llm({"message": "Hello from script!", "priority": "high"})
"Script completed"
""",
        )
        assert "Script result: Script completed" in result
        assert "Wake LLM Contexts" in result
        assert "Hello from script!" in result
        assert "priority" in result
        assert "high" in result

        # Test multiple wake_llm calls
        result = await execute_script_tool(
            ctx,
            """
wake_llm({"action": "first_call", "value": 1})
wake_llm({"action": "second_call", "value": 2}, include_event=False)
{"status": "done", "wake_count": 2}
""",
        )
        assert "Wake Context 1:" in result
        assert "Wake Context 2:" in result
        assert '"action": "first_call"' in result
        assert '"action": "second_call"' in result
        assert "Include Event: True" in result  # First call
        assert "Include Event: False" in result  # Second call
        assert '"wake_count": 2' in result

        # Test script without wake_llm
        result = await execute_script_tool(
            ctx,
            """
# Just a simple calculation
result = 10 + 20
result
""",
        )
        assert "Script result: 30" in result
        assert "Wake LLM Contexts" not in result  # Should not appear if no wake calls

        # Test wake_llm with string context
        result = await execute_script_tool(
            ctx,
            """
wake_llm("Task completed successfully!")
wake_llm("Please review the results", include_event=False)
"Done"
""",
        )
        assert "Script result: Done" in result
        assert "Wake LLM Contexts" in result
        assert '"message": "Task completed successfully!"' in result
        assert '"message": "Please review the results"' in result
        assert "Include Event: True" in result  # First call
        assert "Include Event: False" in result  # Second call
