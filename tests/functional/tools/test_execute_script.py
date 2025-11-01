"""Tests for the execute_script tool."""

from unittest.mock import Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.execute_script import execute_script_tool
from family_assistant.tools.infrastructure import (
    CompositeToolsProvider,
    LocalToolsProvider,
)
from family_assistant.tools.types import ToolExecutionContext


@pytest.mark.asyncio
async def test_execute_script_without_tools_provider(db_engine: AsyncEngine) -> None:
    """Test execute_script when no tools provider is available."""
    async with DatabaseContext(engine=db_engine) as db:
        # Create context without processing service
        ctx = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-conv",
            user_name="test",
            turn_id=None,
            db_context=db,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            processing_service=None,
        )

        # Simple script should work
        result = await execute_script_tool(ctx, 'print("Hello")')
        assert result.text is not None
        assert "Script executed successfully" in result.text or "Hello" in result.text

        # Script using tools should fail
        result = await execute_script_tool(ctx, "tools_list()")
        assert result.text is not None

        assert "Error:" in result.text
        assert result.text is not None

        assert "not found" in result.text


@pytest.mark.asyncio
async def test_execute_script_with_empty_tools_provider(db_engine: AsyncEngine) -> None:
    """Test execute_script with an empty tools provider."""
    async with DatabaseContext(engine=db_engine) as db:
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
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
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
        assert result.text is not None

        assert "Script result: 0" in result.text


@pytest.mark.asyncio
async def test_execute_script_with_tools(db_engine: AsyncEngine) -> None:
    """Test execute_script with actual tools available."""
    async with DatabaseContext(engine=db_engine) as db:
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
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
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
        assert result.text is not None

        assert '"echo"' in result.text  # Just check that echo is in the result

        # Test executing tool
        result = await execute_script_tool(
            ctx,
            """
echo(message="Hello from Starlark!")
""",
        )
        assert result.text is not None

        assert "Echo: Hello from Starlark!" in result.text


@pytest.mark.asyncio
async def test_execute_script_syntax_error(db_engine: AsyncEngine) -> None:
    """Test execute_script with syntax errors."""
    async with DatabaseContext(engine=db_engine) as db:
        ctx = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-conv",
            user_name="test",
            turn_id=None,
            db_context=db,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            processing_service=None,
        )

        # Invalid syntax
        result = await execute_script_tool(ctx, "if true")
        assert result.text is not None

        assert "Error:" in result.text
        assert "syntax" in result.text.lower() or "parse" in result.text.lower()


@pytest.mark.asyncio
async def test_execute_script_with_globals(db_engine: AsyncEngine) -> None:
    """Test execute_script with global variables."""
    async with DatabaseContext(engine=db_engine) as db:
        ctx = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-conv",
            user_name="test",
            turn_id=None,
            db_context=db,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            processing_service=None,
        )

        # Pass globals
        result = await execute_script_tool(
            ctx,
            'user_name + " says " + str(count)',
            globals={"user_name": "Alice", "count": 42},
        )
        assert result.text is not None

        assert "Alice says 42" in result.text


@pytest.mark.asyncio
async def test_execute_script_with_wake_llm(db_engine: AsyncEngine) -> None:
    """Test execute_script with wake_llm calls."""
    async with DatabaseContext(engine=db_engine) as db:
        ctx = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-conv",
            user_name="test",
            turn_id=None,
            db_context=db,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
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
        assert result.text is not None

        assert "Script result: Script completed" in result.text
        assert result.text is not None

        assert "Wake LLM Contexts" in result.text
        assert result.text is not None

        assert "Hello from script!" in result.text
        assert result.text is not None

        assert "priority" in result.text
        assert result.text is not None

        assert "high" in result.text

        # Test multiple wake_llm calls
        result = await execute_script_tool(
            ctx,
            """
wake_llm({"action": "first_call", "value": 1})
wake_llm({"action": "second_call", "value": 2}, include_event=False)
{"status": "done", "wake_count": 2}
""",
        )
        assert result.text is not None

        assert "Wake Context 1:" in result.text
        assert result.text is not None

        assert "Wake Context 2:" in result.text
        assert result.text is not None

        assert '"action": "first_call"' in result.text
        assert result.text is not None

        assert '"action": "second_call"' in result.text
        assert result.text is not None

        assert "Include Event: True" in result.text  # First call
        assert result.text is not None

        assert "Include Event: False" in result.text  # Second call
        assert result.text is not None

        assert '"wake_count": 2' in result.text

        # Test script without wake_llm
        result = await execute_script_tool(
            ctx,
            """
# Just a simple calculation
result = 10 + 20
result
""",
        )
        assert result.text is not None

        assert "Script result: 30" in result.text
        assert result.text is not None

        assert (
            "Wake LLM Contexts" not in result.text
        )  # Should not appear if no wake calls

        # Test wake_llm with string context
        result = await execute_script_tool(
            ctx,
            """
wake_llm("Task completed successfully!")
wake_llm("Please review the results", include_event=False)
"Done"
""",
        )
        assert result.text is not None

        assert "Script result: Done" in result.text
        assert result.text is not None

        assert "Wake LLM Contexts" in result.text
        assert result.text is not None

        assert '"message": "Task completed successfully!"' in result.text
        assert result.text is not None

        assert '"message": "Please review the results"' in result.text
        assert result.text is not None

        assert "Include Event: True" in result.text  # First call
        assert result.text is not None

        assert "Include Event: False" in result.text  # Second call
