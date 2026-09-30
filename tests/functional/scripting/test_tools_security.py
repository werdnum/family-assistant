"""
Tests for tools API security controls in the scripting engine.
"""

from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.scripting.config import ScriptConfig
from family_assistant.scripting.errors import ScriptExecutionError
from family_assistant.scripting.monty_engine import MontyEngine
from family_assistant.storage.database import Database
from family_assistant.tools.types import ToolExecutionContext

from .test_tools_api import MockToolsProvider


@pytest.mark.asyncio
async def test_deny_all_tools(db_engine: AsyncEngine) -> None:
    """Test that deny_all_tools prevents all tool access."""
    # Create config with deny_all_tools
    config = ScriptConfig(deny_all_tools=True)

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
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    # Create engine with security config
    engine = MontyEngine(
        tools_provider=tools_provider,
        config=config,
        default_timezone=ZoneInfo("Australia/Sydney"),
    )

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

    # Test that execute fails (tools_execute is not available when all tools denied)
    script3 = """
tools_execute("echo", message="test")
"""
    with pytest.raises(ScriptExecutionError) as exc_info:
        await engine.evaluate_async(script3, execution_context=context)
    assert "not defined" in str(exc_info.value)


@pytest.mark.asyncio
async def test_allowed_tools_filter(db_engine: AsyncEngine) -> None:
    """Test that allowed_tools filters available tools."""
    # Create config with only "echo" allowed
    config = ScriptConfig(allowed_tools={"echo"})

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
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    # Create engine with security config
    engine = MontyEngine(
        tools_provider=tools_provider,
        config=config,
        default_timezone=ZoneInfo("Australia/Sydney"),
    )

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
@pytest.mark.parametrize(
    "allowed_tools",
    [
        pytest.param(None, id="no_restrictions"),
        pytest.param({"echo", "add_numbers"}, id="every_tool_allowed"),
    ],
)
async def test_all_tools_available_when_none_restricted(
    db_engine: AsyncEngine, allowed_tools: set[str] | None
) -> None:
    """Without restrictions, or with every tool allowed, all tools are available."""
    config = ScriptConfig(allowed_tools=allowed_tools)

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
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    # Create engine with default config
    engine = MontyEngine(
        tools_provider=tools_provider,
        config=config,
        default_timezone=ZoneInfo("Australia/Sydney"),
    )

    # Test that all tools are listed
    script = """
tool_names = [tool["name"] for tool in tools_list()]
tool_names
"""
    result = await engine.evaluate_async(script, execution_context=context)
    assert sorted(result) == ["add_numbers", "echo"]

    # Test that both tools can be executed
    script2 = """
echo_result = tools_execute("echo", message="test")
add_result = tools_execute("add_numbers", a=10, b=5)
[echo_result, add_result]
"""
    result2 = await engine.evaluate_async(script2, execution_context=context)
    assert result2 == ["Echo: test", "Result: 15"]


@pytest.mark.asyncio
async def test_empty_allowed_tools_denies_all(db_engine: AsyncEngine) -> None:
    """Test that an empty allowed_tools set denies all tools."""
    # Create config with empty allowed_tools set
    config = ScriptConfig(allowed_tools=set())

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
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    # Create engine with security config
    engine = MontyEngine(
        tools_provider=tools_provider,
        config=config,
        default_timezone=ZoneInfo("Australia/Sydney"),
    )

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
async def test_deny_all_overrides_allowed_tools(db_engine: AsyncEngine) -> None:
    """Test that deny_all_tools takes precedence over allowed_tools."""
    # Create config with both deny_all and allowed_tools (deny should win)
    config = ScriptConfig(deny_all_tools=True, allowed_tools={"echo", "add_numbers"})

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
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    # Create engine with security config
    engine = MontyEngine(
        tools_provider=tools_provider,
        config=config,
        default_timezone=ZoneInfo("Australia/Sydney"),
    )

    # Test that no tools are available (deny_all wins)
    script = """
tools_list()
"""
    result = await engine.evaluate_async(script, execution_context=context)
    assert result == []

    # Test that execution fails even for "allowed" tools
    # (tools_execute is not available when deny_all_tools=True)
    script2 = """
tools_execute("echo", message="should fail")
"""
    with pytest.raises(ScriptExecutionError) as exc_info:
        await engine.evaluate_async(script2, execution_context=context)
    assert "not defined" in str(exc_info.value)
