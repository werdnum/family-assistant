"""
Tests for tools API security controls in the Starlark scripting engine.
"""

from typing import Any

import pytest

from family_assistant.scripting.engine import StarlarkConfig, StarlarkEngine
from family_assistant.scripting.errors import ScriptExecutionError
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.types import ToolExecutionContext

from .test_tools_api import MockToolsProvider


@pytest.mark.asyncio
async def test_deny_all_tools(test_db_engine: Any) -> None:
    """Test that deny_all_tools prevents all tool access."""
    # Create config with deny_all_tools
    config = StarlarkConfig(deny_all_tools=True)

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

        # Create engine with security config
        engine = StarlarkEngine(tools_provider=tools_provider, config=config)

        # Test that list returns empty
        script = """
tools_list()
"""
        result = await engine.evaluate_async(script, execution_context=context)
        assert result == []

        # Test that get returns None
        script2 = """
tools_get("echo")
"""
        result2 = await engine.evaluate_async(script2, execution_context=context)
        assert result2 is None

        # Test that execute fails
        script3 = """
tools_execute("echo", message="test")
"""
        with pytest.raises(ScriptExecutionError) as exc_info:
            await engine.evaluate_async(script3, execution_context=context)
        assert "not allowed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_allowed_tools_filter(test_db_engine: Any) -> None:
    """Test that allowed_tools filters available tools."""
    # Create config with only "echo" allowed
    config = StarlarkConfig(allowed_tools={"echo"})

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

        # Create engine with security config
        engine = StarlarkEngine(tools_provider=tools_provider, config=config)

        # Test that list only shows allowed tools
        script = """
tools_list = tools_list()
[tool["name"] for tool in tools_list]
"""
        result = await engine.evaluate_async(script, execution_context=context)
        assert result == ["echo"]  # Only echo is allowed

        # Test that get works for allowed tool
        script2 = """
tool = tools_get("echo")
tool["name"] if tool else None
"""
        result2 = await engine.evaluate_async(script2, execution_context=context)
        assert result2 == "echo"

        # Test that get returns None for non-allowed tool
        script3 = """
tool = tools_get("add_numbers")
tool
"""
        result3 = await engine.evaluate_async(script3, execution_context=context)
        assert result3 is None

        # Test that execute works for allowed tool
        script4 = """
tools_execute("echo", message="allowed test")
"""
        result4 = await engine.evaluate_async(script4, execution_context=context)
        assert result4 == "Echo: allowed test"

        # Test that execute fails for non-allowed tool
        script5 = """
tools_execute("add_numbers", a=1, b=2)
"""
        with pytest.raises(ScriptExecutionError) as exc_info:
            await engine.evaluate_async(script5, execution_context=context)
        assert "not allowed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_no_restrictions_by_default(test_db_engine: Any) -> None:
    """Test that without restrictions, all tools are available."""
    # Create default config (no restrictions)
    config = StarlarkConfig()

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

        # Create engine with default config
        engine = StarlarkEngine(tools_provider=tools_provider, config=config)

        # Test that all tools are listed
        script = """
tools_list = tools_list()
sorted([tool["name"] for tool in tools_list])
"""
        result = await engine.evaluate_async(script, execution_context=context)
        assert result == ["add_numbers", "echo"]

        # Test that both tools can be executed
        script2 = """
echo_result = tools_execute("echo", message="test")
add_result = tools_execute("add_numbers", a=10, b=5)
[echo_result, add_result]
"""
        result2 = await engine.evaluate_async(script2, execution_context=context)
        assert result2 == ["Echo: test", "Result: 15"]


@pytest.mark.asyncio
async def test_empty_allowed_tools_denies_all(test_db_engine: Any) -> None:
    """Test that an empty allowed_tools set denies all tools."""
    # Create config with empty allowed_tools set
    config = StarlarkConfig(allowed_tools=set())

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

        # Create engine with security config
        engine = StarlarkEngine(tools_provider=tools_provider, config=config)

        # Test that list returns empty
        script = """
tools_list()
"""
        result = await engine.evaluate_async(script, execution_context=context)
        assert result == []

        # Test that execute fails
        script2 = """
tools_execute("echo", message="test")
"""
        with pytest.raises(ScriptExecutionError) as exc_info:
            await engine.evaluate_async(script2, execution_context=context)
        assert "not allowed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_security_logging(test_db_engine: Any, caplog: Any) -> None:
    """Test that security events are logged properly."""
    # Create config with restrictions
    config = StarlarkConfig(allowed_tools={"echo"})

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

        # Create engine with security config
        engine = StarlarkEngine(tools_provider=tools_provider, config=config)

        # Clear existing logs
        caplog.clear()

        # Try to execute a denied tool (this should fail)
        script = """
# Try to execute add_numbers which is not in allowed_tools
result = tools_execute("add_numbers", a=1, b=2)
result
"""
        # We expect an exception because add_numbers is not allowed
        with pytest.raises(ScriptExecutionError) as exc_info:
            await engine.evaluate_async(script, execution_context=context)

        # Check that the error message mentions the tool is not allowed
        assert "Tool 'add_numbers' is not allowed" in str(exc_info.value)

        # Check that security warning was logged
        assert any(
            "Security: Attempted execution of denied tool 'add_numbers'"
            in record.message
            for record in caplog.records
        )


@pytest.mark.asyncio
async def test_multiple_allowed_tools(test_db_engine: Any) -> None:
    """Test configuration with multiple allowed tools."""
    # Create config with both tools allowed explicitly
    config = StarlarkConfig(allowed_tools={"echo", "add_numbers"})

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

        # Create engine with security config
        engine = StarlarkEngine(tools_provider=tools_provider, config=config)

        # Test that both tools are available
        script = """
tools_list = tools_list()
sorted([tool["name"] for tool in tools_list])
"""
        result = await engine.evaluate_async(script, execution_context=context)
        assert result == ["add_numbers", "echo"]

        # Test that both can be executed
        script2 = """
results = []
results.append(tools_execute("echo", message="multi test"))
results.append(tools_execute("add_numbers", a=7, b=3))
results
"""
        result2 = await engine.evaluate_async(script2, execution_context=context)
        assert result2 == ["Echo: multi test", "Result: 10"]


@pytest.mark.asyncio
async def test_deny_all_overrides_allowed_tools(test_db_engine: Any) -> None:
    """Test that deny_all_tools takes precedence over allowed_tools."""
    # Create config with both deny_all and allowed_tools (deny should win)
    config = StarlarkConfig(deny_all_tools=True, allowed_tools={"echo", "add_numbers"})

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

        # Create engine with security config
        engine = StarlarkEngine(tools_provider=tools_provider, config=config)

        # Test that no tools are available (deny_all wins)
        script = """
tools_list()
"""
        result = await engine.evaluate_async(script, execution_context=context)
        assert result == []

        # Test that execution fails even for "allowed" tools
        script2 = """
tools_execute("echo", message="should fail")
"""
        with pytest.raises(ScriptExecutionError) as exc_info:
            await engine.evaluate_async(script2, execution_context=context)
        assert "not allowed" in str(exc_info.value)
