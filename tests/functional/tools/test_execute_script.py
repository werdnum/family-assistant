"""Tests for the execute_script tool."""

import logging
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, KeychuteConfig
from family_assistant.scripting.errors import ScriptTimeoutError
from family_assistant.security.taint import TurnTaintState
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database
from family_assistant.tools.data_visualization import create_vega_chart_tool
from family_assistant.tools.execute_script import (
    SCRIPT_TOOLS_DEFINITION,
    execute_script_tool,
)
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


def test_execute_script_tool_description_points_to_scripting_guide() -> None:
    """Detailed scripting guidance is loaded on demand instead of sent every turn."""
    function = SCRIPT_TOOLS_DEFINITION[0]["function"]
    description = function["description"]

    assert "scripting.md" in description
    assert "get_user_documentation_content" in description
    assert "Example Scripts" not in description
    assert "Sandbox Limitations" not in description
    assert len(description) < 600


@pytest.mark.asyncio
async def test_execute_script_without_tools_provider(db_engine: AsyncEngine) -> None:
    """Test execute_script when no tools provider is available."""
    db = Database(engine=db_engine)
    # Create context without processing service
    ctx = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-conv",
        user_name="test",
        turn_id=None,
        db_context=db,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        processing_service=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
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

    assert "not found" in result.text or "not defined" in result.text


@pytest.mark.asyncio
async def test_execute_script_with_empty_tools_provider(db_engine: AsyncEngine) -> None:
    """Test execute_script with an empty tools provider."""
    db = Database(engine=db_engine)
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
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        processing_service=mock_service,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
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
async def test_execute_script_exposes_configured_keychute(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configured scripts can consume a response without receiving a credential."""

    async def fake_request(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"status_code": 200, "headers": {}, "body": b'{"answer": 42}'}

    monkeypatch.setattr(
        "family_assistant.scripting.apis.keychute.KeychuteScriptHttpClient.request",
        fake_request,
    )
    db = Database(engine=db_engine)
    mock_service = Mock()
    mock_service.tools_provider = CompositeToolsProvider([])
    mock_service.app_config = AppConfig(
        keychute_config=KeychuteConfig(
            enabled=True,
            url="https://keychute.test",
            token=SecretStr("client-token"),
        )
    )
    ctx = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-conv",
        user_name="test",
        turn_id=None,
        db_context=db,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        processing_service=mock_service,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    result = await execute_script_tool(
        ctx,
        'json_decode(keychute_http_request("api", "https://example.test")["body"])["answer"]',
    )

    assert result.text == "Script result: 42"


@pytest.mark.asyncio
async def test_execute_script_with_tools(db_engine: AsyncEngine) -> None:
    """Test execute_script with actual tools available."""
    db = Database(engine=db_engine)
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
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        processing_service=mock_service,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
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
echo(message="Hello from script!")
""",
    )
    assert result.text is not None

    assert "Echo: Hello from script!" in result.text


@pytest.mark.asyncio
async def test_execute_script_syntax_error(db_engine: AsyncEngine) -> None:
    """Test execute_script with syntax errors."""
    db = Database(engine=db_engine)
    ctx = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-conv",
        user_name="test",
        turn_id=None,
        db_context=db,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        processing_service=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    # Invalid syntax
    result = await execute_script_tool(ctx, "if true")
    assert result.text is not None

    assert "Error:" in result.text
    assert "syntax" in result.text.lower() or "parse" in result.text.lower()
    assert isinstance(result.data, dict)
    assert result.data["status"] == "error"
    assert result.data["error_type"] == "syntax_error"
    assert "syntax" in result.data["error"].lower()


@pytest.mark.asyncio
async def test_execute_script_with_globals(db_engine: AsyncEngine) -> None:
    """Test execute_script with global variables."""
    db = Database(engine=db_engine)
    ctx = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-conv",
        user_name="test",
        turn_id=None,
        db_context=db,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        processing_service=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
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
    db = Database(engine=db_engine)
    ctx = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-conv",
        user_name="test",
        turn_id=None,
        db_context=db,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        processing_service=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
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

    assert "Wake LLM Contexts" not in result.text  # Should not appear if no wake calls

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
    (per design doc: scripts return dicts due to JSON constraint)
    but process_attachment_arguments doesn't handle them, causing AttributeError.
    """
    db = Database(engine=db_engine)
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
            taint_state=TurnTaintState.empty(),
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
                        "debug": {"type": "boolean"},
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
        plugins=None,
        event_sources=None,
        attachment_registry=attachment_registry,
        processing_service=mock_service,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    # Pass the script-facing result of one tool to a second tool that reads its attachment.
    script = """
data_result = get_test_data()
spec_json = '{"$schema": "https://vega.github.io/schema/vega-lite/v5.json", "data": {"name": "test_data.json"}, "mark": "line", "encoding": {"x": {"field": "x", "type": "quantitative"}, "y": {"field": "y", "type": "quantitative"}}}'
chart = create_vega_chart(
    spec=spec_json,
    data_attachments=[data_result],
    debug=True
)

chart
"""

    result = await execute_script_tool(ctx, script)

    assert isinstance(result.data, dict), result.text
    assert result.data["data"]["values"] == [{"x": 1, "y": 2}, {"x": 2, "y": 4}]


@pytest.mark.asyncio
async def test_execute_script_returns_print_output(db_engine: AsyncEngine) -> None:
    """print() output is surfaced to the LLM under a Script Output section."""
    db = Database(engine=db_engine)
    ctx = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-conv",
        user_name="test",
        turn_id=None,
        db_context=db,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        processing_service=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    script = """
print("step one")
print("step two")
42
"""
    result = await execute_script_tool(ctx, script)

    assert result.text is not None
    assert "--- Script Output ---" in result.text
    assert "step one" in result.text
    assert "step two" in result.text
    # Both the printed output and the return value are present.
    assert "Script result: 42" in result.text
    # Printed output appears ahead of the return value.
    assert result.text.index("step one") < result.text.index("Script result: 42")
    # The structured data still holds the script's return value, not the output.
    assert result.data == 42


@pytest.mark.asyncio
async def test_execute_script_no_output_section_without_print(
    db_engine: AsyncEngine,
) -> None:
    """Scripts that print nothing do not get a Script Output section."""
    db = Database(engine=db_engine)
    ctx = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-conv",
        user_name="test",
        turn_id=None,
        db_context=db,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        processing_service=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    result = await execute_script_tool(ctx, "1 + 1")

    assert result.text is not None
    assert "--- Script Output ---" not in result.text
    assert "Script result: 2" in result.text


@pytest.mark.asyncio
async def test_execute_script_includes_output_on_failure(
    db_engine: AsyncEngine,
) -> None:
    """Output printed before a runtime failure is included with the error."""
    db = Database(engine=db_engine)
    ctx = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-conv",
        user_name="test",
        turn_id=None,
        db_context=db,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        processing_service=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    script = """
print("printed before crash")
1 / 0
"""
    result = await execute_script_tool(ctx, script)

    assert result.text is not None
    assert "printed before crash" in result.text
    assert "Error:" in result.text
    assert isinstance(result.data, dict)
    assert result.data["status"] == "error"


@pytest.mark.asyncio
async def test_execute_script_truncates_huge_output(db_engine: AsyncEngine) -> None:
    """A script printing far too much has its output truncated, not unbounded."""
    db = Database(engine=db_engine)
    ctx = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-conv",
        user_name="test",
        turn_id=None,
        db_context=db,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        processing_service=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    # Print well beyond the 16 KiB default cap.
    script = """
i = 0
while i < 5000:
    print("0123456789ABCDEF")
    i = i + 1
0
"""
    result = await execute_script_tool(ctx, script)

    assert result.text is not None
    assert "... [output truncated] ..." in result.text
    # The surfaced text stays bounded rather than echoing all 5000 lines.
    assert len(result.text) < 32 * 1024


def _logging_test_context(db_engine: AsyncEngine) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="test-conv",
        user_name="test",
        turn_id=None,
        db_context=Database(engine=db_engine),
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        processing_service=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


def _error_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and r.name.startswith("family_assistant")
    ]


def _user_script_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == "family_assistant.tools.execute_script"
        and getattr(r, "error_category", None) == "user_script"
    ]


@pytest.mark.asyncio
async def test_execute_script_runtime_error_logs_at_info(
    db_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = _logging_test_context(db_engine)

    with caplog.at_level(logging.INFO):
        result = await execute_script_tool(ctx, "a + b", globals={"a": "hello", "b": 1})

    assert result.text is not None
    assert "Error: Script execution failed: TypeError" in result.text
    assert "Script execution failed: Script execution failed:" not in result.text
    assert isinstance(result.data, dict)
    assert result.data["error_type"] == "execution_error"
    assert not _error_records(caplog)
    records = _user_script_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.INFO


@pytest.mark.asyncio
async def test_execute_script_syntax_error_does_not_log_at_error(
    db_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = _logging_test_context(db_engine)

    with caplog.at_level(logging.INFO):
        result = await execute_script_tool(ctx, "if True")

    assert isinstance(result.data, dict)
    assert result.data["error_type"] == "syntax_error"
    assert not _error_records(caplog)


@pytest.mark.asyncio
async def test_execute_script_host_function_misuse_does_not_log_at_error(
    db_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A host function rejecting the script's arguments is the script's mistake."""
    ctx = _logging_test_context(db_engine)

    def picky(value: int) -> int:
        raise ValueError(f"bad value: {value}")

    with caplog.at_level(logging.INFO):
        result = await execute_script_tool(ctx, "picky(3)", globals={"picky": picky})

    assert isinstance(result.data, dict)
    assert result.data["error_type"] == "execution_error"
    assert "bad value: 3" in result.data["error"]
    assert not _error_records(caplog)


@pytest.mark.asyncio
async def test_execute_script_host_function_failure_logs_at_error(
    db_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A host function failing on its own (a provider outage, say) stays visible."""
    ctx = _logging_test_context(db_engine)

    def flaky_backend() -> str:
        raise RuntimeError("provider unavailable")

    with caplog.at_level(logging.INFO):
        result = await execute_script_tool(
            ctx, "flaky_backend()", globals={"flaky_backend": flaky_backend}
        )

    assert isinstance(result.data, dict)
    assert result.data["error_type"] == "execution_error"
    assert "provider unavailable" in result.data["error"]
    error_records = _error_records(caplog)
    assert len(error_records) == 1
    assert "flaky_backend" in error_records[0].getMessage()
    assert error_records[0].exc_info is not None


@pytest.mark.asyncio
async def test_execute_script_unexpected_engine_exception_logs_at_error(
    db_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = _logging_test_context(db_engine)

    with (
        patch(
            "family_assistant.tools.execute_script.MontyEngine.evaluate_async",
            side_effect=RuntimeError("Unexpected host failure"),
        ),
        caplog.at_level(logging.INFO),
    ):
        result = await execute_script_tool(ctx, "1 + 1")

    assert result.text is not None
    assert "Unexpected error executing script: Unexpected host failure" in result.text
    error_records = _error_records(caplog)
    assert len(error_records) == 1
    assert (
        "Unexpected error executing script: Unexpected host failure"
        in error_records[0].getMessage()
    )


@pytest.mark.asyncio
async def test_execute_script_timeout_downgraded_to_warning(
    db_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Script timeouts log at WARNING with user_script category, not ERROR."""
    ctx = _logging_test_context(db_engine)

    with (
        patch(
            "family_assistant.tools.execute_script.MontyEngine.evaluate_async",
            side_effect=ScriptTimeoutError(
                "Script execution timed out after 5.0 seconds", 5.0
            ),
        ),
        caplog.at_level(logging.INFO),
    ):
        result = await execute_script_tool(ctx, "while True: pass")
        assert result.text is not None
        assert "timed out after 5.0 seconds" in result.text
        assert isinstance(result.data, dict)
        assert result.data["error_type"] == "timeout_error"

    timeout_records = [
        r
        for r in caplog.records
        if r.name == "family_assistant.tools.execute_script"
        and getattr(r, "error_category", None) == "user_script"
    ]
    assert len(timeout_records) == 1
    assert timeout_records[0].levelno == logging.WARNING
