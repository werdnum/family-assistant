"""Unit tests for Monty script type checking."""

from typing import TYPE_CHECKING

import pytest

from family_assistant.scripting.config import ScriptConfig
from family_assistant.scripting.errors import ScriptSyntaxError, ScriptTypingError
from family_assistant.scripting.monty_engine import MontyEngine
from family_assistant.scripting.type_stubs import (
    generate_prefix_code,
    generate_tool_stub,
)

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolDefinition


class TestTypeStubGeneration:
    """Tests for type stub generation from tool definitions."""

    def test_simple_tool_stub(self) -> None:
        tool_def: ToolDefinition = {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Echo a message",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "The message"},
                    },
                    "required": ["message"],
                },
            },
        }
        stub = generate_tool_stub(tool_def)
        assert stub == "def echo(*, message: str) -> str: ..."

    def test_optional_parameters(self) -> None:
        tool_def: ToolDefinition = {
            "type": "function",
            "function": {
                "name": "search",
                "description": "Search",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["query"],
                },
            },
        }
        stub = generate_tool_stub(tool_def)
        assert "query: str" in stub
        assert "limit: int | None = None" in stub

    def test_multiple_types(self) -> None:
        tool_def: ToolDefinition = {
            "type": "function",
            "function": {
                "name": "process",
                "description": "Process data",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "number"},
                        "flag": {"type": "boolean"},
                        "items": {"type": "array"},
                        "data": {"type": "object"},
                    },
                    "required": ["value"],
                },
            },
        }
        stub = generate_tool_stub(tool_def)
        assert "value: float" in stub
        assert "flag: bool" in stub
        assert "items: list" in stub
        assert "data: dict" in stub

    def test_no_parameters(self) -> None:
        tool_def: ToolDefinition = {
            "type": "function",
            "function": {
                "name": "get_status",
                "description": "Get status",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
        stub = generate_tool_stub(tool_def)
        assert stub == "def get_status() -> str: ..."

    def test_generate_prefix_code_includes_apis(self) -> None:
        prefix = generate_prefix_code(include_apis=True)
        assert "def time_now()" in prefix
        assert "def json_encode(" in prefix
        assert "def wake_llm(" in prefix
        assert "NANOSECOND: float" in prefix

    def test_generate_prefix_code_without_apis(self) -> None:
        prefix = generate_prefix_code(include_apis=False, include_tools_api=False)
        assert "def time_now()" not in prefix

    def test_generate_prefix_code_with_tools(self) -> None:
        tool_defs: list[ToolDefinition] = [
            {
                "type": "function",
                "function": {
                    "name": "my_tool",
                    "description": "A tool",
                    "parameters": {
                        "type": "object",
                        "properties": {"x": {"type": "integer"}},
                        "required": ["x"],
                    },
                },
            }
        ]
        prefix = generate_prefix_code(tool_definitions=tool_defs)
        assert "def my_tool(*, x: int) -> str: ..." in prefix
        assert "def tool_my_tool(*, x: int) -> str: ..." in prefix


class TestMontyTypeCheck:
    """Tests for MontyEngine.type_check() integration."""

    @pytest.mark.asyncio
    async def test_valid_script_passes(self) -> None:
        engine = MontyEngine(config=ScriptConfig(disable_apis=False))
        await engine.type_check("now = time_now()\nhour = time_hour(now)")

    @pytest.mark.asyncio
    async def test_type_error_raises(self) -> None:
        engine = MontyEngine(config=ScriptConfig(disable_apis=False))
        with pytest.raises(ScriptTypingError, match="invalid-argument-type"):
            await engine.type_check("time_hour(123)")

    @pytest.mark.asyncio
    async def test_syntax_error_raises(self) -> None:
        engine = MontyEngine()
        with pytest.raises(ScriptSyntaxError):
            await engine.type_check("print('hello'")

    @pytest.mark.asyncio
    async def test_basic_expressions_pass(self) -> None:
        engine = MontyEngine(config=ScriptConfig(disable_apis=True))
        await engine.type_check("x = 2 + 3\nx")

    @pytest.mark.asyncio
    async def test_json_api_types(self) -> None:
        engine = MontyEngine(config=ScriptConfig(disable_apis=False))
        await engine.type_check('encoded = json_encode({"key": "value"})')

    @pytest.mark.asyncio
    async def test_disabled_apis_skips_stubs(self) -> None:
        engine = MontyEngine(config=ScriptConfig(disable_apis=True))
        with pytest.raises(ScriptTypingError):
            await engine.type_check("time_now()")
