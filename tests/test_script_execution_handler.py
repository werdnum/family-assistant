"""Tests for the script_execution task handler."""

from unittest.mock import Mock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.scripting import ScriptError, ScriptTimeoutError
from family_assistant.task_worker import handle_script_execution
from family_assistant.tools import ToolExecutionContext
from family_assistant.utils.clock import SystemClock


@pytest.mark.asyncio
async def test_script_execution_handler_success(test_db_engine: AsyncEngine) -> None:
    """Test successful script execution."""
    # Create mock execution context
    mock_db_context = Mock()
    mock_processing_service = Mock()
    mock_processing_service.tools_provider = Mock()

    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-123",
        user_name="test_user",
        turn_id="turn-123",
        db_context=mock_db_context,
        chat_interface=Mock(),
        timezone_str="UTC",
        processing_service=mock_processing_service,
        embedding_generator=Mock(),
        clock=SystemClock(),
    )

    # Create payload
    payload = {
        "script_code": "result = 1 + 1",
        "event_data": {"test": "data"},
        "listener_id": "listener-123",
        "conversation_id": "test-123",
        "config": {"timeout": 5, "listener_name": "Test Listener"},
    }

    # Mock StarlarkEngine
    with patch("family_assistant.scripting.StarlarkEngine") as mock_engine_class:
        mock_engine = Mock()
        mock_engine.evaluate.return_value = 2
        mock_engine_class.return_value = mock_engine

        # Execute handler
        await handle_script_execution(exec_context, payload)

        # Verify engine was created with correct config
        mock_engine_class.assert_called_once()
        call_kwargs = mock_engine_class.call_args[1]
        assert call_kwargs["tools_provider"] == mock_processing_service.tools_provider
        assert call_kwargs["config"].max_execution_time == 5

        # Verify evaluate was called with correct globals
        mock_engine.evaluate.assert_called_once()
        call_args = mock_engine.evaluate.call_args[0]
        assert call_args[0] == "result = 1 + 1"  # script code

        globals_dict = call_args[1]
        assert globals_dict["event"] == {"test": "data"}
        assert globals_dict["conversation_id"] == "test-123"
        assert globals_dict["listener_id"] == "listener-123"
        assert globals_dict["listener_name"] == "Test Listener"


@pytest.mark.asyncio
async def test_script_execution_handler_timeout(test_db_engine: AsyncEngine) -> None:
    """Test script execution timeout handling."""
    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-123",
        user_name="test_user",
        turn_id="turn-123",
        db_context=Mock(),
        chat_interface=Mock(),
        timezone_str="UTC",
        processing_service=Mock(),
        embedding_generator=Mock(),
        clock=SystemClock(),
    )

    payload = {
        "script_code": "while True: pass",
        "event_data": {},
        "listener_id": "listener-123",
        "conversation_id": "test-123",
    }

    with patch("family_assistant.scripting.StarlarkEngine") as mock_engine_class:
        mock_engine = Mock()
        mock_engine.evaluate.side_effect = ScriptTimeoutError(
            "Script timed out", timeout_seconds=10
        )
        mock_engine_class.return_value = mock_engine

        # Should raise ScriptTimeoutError for retry
        with pytest.raises(ScriptTimeoutError):
            await handle_script_execution(exec_context, payload)


@pytest.mark.asyncio
async def test_script_execution_handler_error(test_db_engine: AsyncEngine) -> None:
    """Test script execution error handling."""
    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-123",
        user_name="test_user",
        turn_id="turn-123",
        db_context=Mock(),
        chat_interface=Mock(),
        timezone_str="UTC",
        processing_service=Mock(),
        embedding_generator=Mock(),
        clock=SystemClock(),
    )

    payload = {
        "script_code": "invalid python syntax !!!",
        "event_data": {},
        "listener_id": "listener-123",
        "conversation_id": "test-123",
    }

    with patch("family_assistant.scripting.StarlarkEngine") as mock_engine_class:
        mock_engine = Mock()
        mock_engine.evaluate.side_effect = ScriptError("Syntax error")
        mock_engine_class.return_value = mock_engine

        # Should raise ScriptError for retry
        with pytest.raises(ScriptError):
            await handle_script_execution(exec_context, payload)


@pytest.mark.asyncio
async def test_script_execution_handler_missing_fields(
    test_db_engine: AsyncEngine,
) -> None:
    """Test handler with missing required fields."""
    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-123",
        user_name="test_user",
        turn_id="turn-123",
        db_context=Mock(),
        chat_interface=Mock(),
        timezone_str="UTC",
        processing_service=Mock(),
        embedding_generator=Mock(),
        clock=SystemClock(),
    )

    # Missing script_code
    payload = {
        "event_data": {},
        "listener_id": "listener-123",
        "conversation_id": "test-123",
    }

    with pytest.raises(
        ValueError, match="Missing required field in payload: script_code"
    ):
        await handle_script_execution(exec_context, payload)

    # Missing listener_id
    payload = {
        "script_code": "pass",
        "event_data": {},
        "conversation_id": "test-123",
    }

    with pytest.raises(
        ValueError, match="Missing required field in payload: listener_id"
    ):
        await handle_script_execution(exec_context, payload)


@pytest.mark.asyncio
async def test_script_execution_handler_no_tools_provider(
    test_db_engine: AsyncEngine,
) -> None:
    """Test handler when no tools provider is available."""
    mock_processing_service = Mock()
    # No tools_provider attribute
    del mock_processing_service.tools_provider

    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-123",
        user_name="test_user",
        turn_id="turn-123",
        db_context=Mock(),
        chat_interface=Mock(),
        timezone_str="UTC",
        processing_service=mock_processing_service,
        embedding_generator=Mock(),
        clock=SystemClock(),
    )

    payload = {
        "script_code": "result = 1 + 1",
        "event_data": {},
        "listener_id": "listener-123",
        "conversation_id": "test-123",
    }

    with patch("family_assistant.scripting.StarlarkEngine") as mock_engine_class:
        mock_engine = Mock()
        mock_engine.evaluate.return_value = 2
        mock_engine_class.return_value = mock_engine

        # Should still work, just with no tools
        await handle_script_execution(exec_context, payload)

        # Verify engine was created with None tools_provider
        call_kwargs = mock_engine_class.call_args[1]
        assert call_kwargs["tools_provider"] is None


@pytest.mark.asyncio
async def test_script_execution_handler_unexpected_error(
    test_db_engine: AsyncEngine,
) -> None:
    """Test handling of unexpected errors during script execution."""
    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-123",
        user_name="test_user",
        turn_id="turn-123",
        db_context=Mock(),
        chat_interface=Mock(),
        timezone_str="UTC",
        processing_service=Mock(),
        embedding_generator=Mock(),
        clock=SystemClock(),
    )

    payload = {
        "script_code": "result = 1 + 1",
        "event_data": {},
        "listener_id": "listener-123",
        "conversation_id": "test-123",
    }

    with patch("family_assistant.scripting.StarlarkEngine") as mock_engine_class:
        mock_engine = Mock()
        mock_engine.evaluate.side_effect = RuntimeError("Unexpected error")
        mock_engine_class.return_value = mock_engine

        # Should wrap in ScriptError
        with pytest.raises(ScriptError, match="Unexpected error: Unexpected error"):
            await handle_script_execution(exec_context, payload)
