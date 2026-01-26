"""Tests for the execute_script tool."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.data_visualization import create_vega_chart_tool
from family_assistant.tools.execute_script import execute_script_tool
from family_assistant.tools.infrastructure import (
    CompositeToolsProvider,
    LocalToolsProvider,
)
from family_assistant.tools.types import (
    ToolAttachment,
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
)


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
            camera_backend=None,
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
            camera_backend=None,
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
        tool_definitions: list[ToolDefinition] = [
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
            camera_backend=None,
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
            camera_backend=None,
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
            camera_backend=None,
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
            camera_backend=None,
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


@pytest.mark.asyncio
async def test_script_attachment_composition_dict_format(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    """
    Test passing attachment from one tool to another via script (dict format).

    This reproduces the bug where scripts pass dict-based attachments
    (per design doc: scripts return dicts due to starlark-pyo3 JSON constraint)
    but process_attachment_arguments doesn't handle them, causing AttributeError.
    """
    async with DatabaseContext(engine=db_engine) as db:
        # Create attachment registry
        test_storage = tmp_path / "test_attachments"
        test_storage.mkdir(exist_ok=True)
        attachment_registry = AttachmentRegistry(
            storage_path=str(test_storage), db_engine=db_engine, config=None
        )

        # Create a mock tool that returns ToolResult with attachments
        # This simulates tools like download_state_history
        async def mock_data_tool(exec_context: ToolExecutionContext) -> ToolResult:
            """Mock tool that returns data as an attachment."""
            # Store test data as attachment
            test_data = '[{"x": 1, "y": 2}, {"x": 2, "y": 4}]'
            metadata = await attachment_registry.store_and_register_tool_attachment(
                file_content=test_data.encode("utf-8"),
                filename="test_data.json",
                content_type="text/plain",
                tool_name="get_test_data",
                description="Test data",
                conversation_id="test-conv",
            )

            # Return ToolResult with attachment (like download_state_history does)
            return ToolResult(
                text="Test data with 2 points",
                attachments=[
                    ToolAttachment(
                        attachment_id=str(metadata.attachment_id),
                        mime_type="text/plain",
                    )
                ],
            )

        # Create tools provider with both tools
        tool_definitions: list[ToolDefinition] = [
            {
                "type": "function",
                "function": {
                    "name": "get_test_data",
                    "description": "Get test data",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_vega_chart",
                    "description": "Create a chart",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "spec": {"type": "string"},
                            "data_attachments": {
                                "type": "array",
                                "items": {"type": "attachment"},
                            },
                        },
                        "required": ["spec"],
                    },
                },
            },
        ]

        local_provider = LocalToolsProvider(
            definitions=tool_definitions,
            implementations={
                "get_test_data": mock_data_tool,
                "create_vega_chart": create_vega_chart_tool,
            },
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
            attachment_registry=attachment_registry,
            processing_service=mock_service,
            camera_backend=None,
        )

        # Script that calls a tool returning ToolResult with attachments,
        # then passes that result to create_vega_chart
        # The tool result gets munged to a ScriptToolResult dict: {"text": "...", "attachments": [...]}
        # This should fail with: 'dict' object has no attribute 'get_content_async'
        script = """
# Call mock tool that returns ToolResult with attachments
# This gets munged to a ScriptToolResult dict by ToolsAPI
data_result = get_test_data()

# Create Vega-Lite spec as JSON string
spec_json = '{"$schema": "https://vega.github.io/schema/vega-lite/v5.json", "data": {"name": "data.json"}, "mark": "line", "encoding": {"x": {"field": "x", "type": "quantitative"}, "y": {"field": "y", "type": "quantitative"}}}'

# Pass the ScriptToolResult dict to create_vega_chart
# data_result is {"text": "Test data with 2 points", "attachments": [{"id": "..."}]}
# This will fail because process_attachment_arguments doesn't handle ScriptToolResult dicts
chart = create_vega_chart(
    spec=spec_json,
    data_attachments=[data_result]  # data_result is a ScriptToolResult dict!
)

chart
"""

        # Execute the script - should now work correctly with the fix
        result = await execute_script_tool(ctx, script)

        # After fix: should succeed without errors
        assert result.text is not None
        # Should not have AttributeError
        assert "AttributeError" not in result.text, f"Unexpected error: {result.text}"
        assert "'dict' object has no attribute" not in result.text, (
            f"Unexpected error: {result.text}"
        )
        # Should contain indication of success
        assert "Error:" not in result.text or "Script result:" in result.text
