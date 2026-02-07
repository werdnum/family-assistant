"""
Tests for the scripting engine integration.

This module tests our integration with both the Starlark and Monty engines,
validating that both implement the same interface correctly.
"""

import asyncio
from typing import Any

import pytest

from family_assistant.scripting.engine import StarlarkEngine
from family_assistant.scripting.errors import (
    ScriptExecutionError,
    ScriptSyntaxError,
)
from family_assistant.scripting.monty_engine import MontyEngine


class TestEngineIntegration:
    """Test engine integration with both Starlark and Monty."""

    @pytest.mark.asyncio
    async def test_basic_evaluation(self, engine_class: type) -> None:
        """Test that we can evaluate a simple expression."""
        engine = engine_class()
        result = await engine.evaluate_async("2 + 3")
        assert result == 5

    @pytest.mark.asyncio
    async def test_global_variable_injection(self, engine_class: type) -> None:
        """Test that we can inject Python data structures as globals."""
        engine = engine_class()

        globals_dict = {
            "name": "Alice",
            "age": 30,
            "data": {"key": "value", "count": 42},
            "items": [1, 2, 3, 4, 5],
        }

        assert await engine.evaluate_async("name", globals_dict) == "Alice"
        assert await engine.evaluate_async("age * 2", globals_dict) == 60
        assert await engine.evaluate_async("data['count']", globals_dict) == 42
        assert await engine.evaluate_async("len(items)", globals_dict) == 5

    @pytest.mark.asyncio
    async def test_syntax_error_handling(self, engine_class: type) -> None:
        """Test that syntax errors are properly converted to ScriptSyntaxError."""
        engine = engine_class()

        with pytest.raises(ScriptSyntaxError) as exc_info:
            await engine.evaluate_async("print('hello'")

        assert isinstance(exc_info.value, ScriptSyntaxError)

    @pytest.mark.asyncio
    async def test_runtime_error_handling(self, engine_class: type) -> None:
        """Test that runtime errors are properly converted to ScriptExecutionError."""
        engine = engine_class()

        with pytest.raises(ScriptExecutionError):
            await engine.evaluate_async("1 / 0")

        with pytest.raises(ScriptExecutionError):
            await engine.evaluate_async("undefined_variable")

    @pytest.mark.asyncio
    async def test_async_evaluation(self, engine_class: type) -> None:
        """Test asynchronous script evaluation."""
        engine = engine_class()

        result = await engine.evaluate_async("10 + 20")
        assert result == 30

        globals_dict = {"x": 5, "y": 10}
        result = await engine.evaluate_async("x * y", globals_dict)
        assert result == 50

    @pytest.mark.skip(reason="PERMANENTLY DISABLED: Resource-intensive timeout test.")
    @pytest.mark.asyncio
    async def test_async_timeout(self, engine_class: type) -> None:
        """Test that long-running scripts timeout in async mode."""
        pass

    @pytest.mark.asyncio
    async def test_concurrent_execution(self, engine_class: type) -> None:
        """Test that multiple scripts can execute concurrently."""
        engine = engine_class()

        scripts = [
            ("2 + 3", 5),
            ("'hello' + ' ' + 'world'", "hello world"),
            ("[x * 2 for x in [1, 2, 3]]", [2, 4, 6]),
        ]

        tasks = [engine.evaluate_async(script) for script, _ in scripts]
        results = await asyncio.gather(*tasks)

        for i, (_, expected) in enumerate(scripts):
            assert results[i] == expected

    @pytest.mark.asyncio
    async def test_empty_script_handling(self, engine_class: type) -> None:
        """Test that empty scripts are handled gracefully."""
        engine = engine_class()
        assert await engine.evaluate_async("") is None
        assert await engine.evaluate_async("   \n  \t  \n  ") is None

    @pytest.mark.asyncio
    async def test_complex_data_structure_result(self, engine_class: type) -> None:
        """Test that complex data structures are properly returned."""
        engine = engine_class()

        result = await engine.evaluate_async('{"name": "test", "values": [1, 2, 3]}')
        assert result == {"name": "test", "values": [1, 2, 3]}

        result = await engine.evaluate_async('[{"id": 1}, {"id": 2}]')
        assert result == [{"id": 1}, {"id": 2}]

    @pytest.mark.asyncio
    async def test_function_injection_supported(self, engine_class: type) -> None:
        """Test that function injection is supported."""
        engine = engine_class()

        def my_function() -> int:
            return 42

        globals_dict = {"my_func": my_function, "my_value": 10}

        assert await engine.evaluate_async("my_value", globals_dict) == 10
        assert await engine.evaluate_async("my_func()", globals_dict) == 42

        def add(x: int, y: int) -> int:
            return x + y

        globals_dict["add"] = add
        assert await engine.evaluate_async("add(5, 3)", globals_dict) == 8

        def concat(*args: str) -> str:
            return "".join(args)

        globals_dict["concat"] = concat
        assert (
            await engine.evaluate_async("concat('Hello', ' ', 'World')", globals_dict)
            == "Hello World"
        )

        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        def get_data() -> dict[str, Any]:
            return {"status": "ok", "count": 3}

        globals_dict["get_data"] = get_data
        assert await engine.evaluate_async("get_data()['status']", globals_dict) == "ok"
        assert await engine.evaluate_async("get_data()['count']", globals_dict) == 3

    @pytest.mark.skip(
        reason="PERMANENTLY DISABLED: Resource limit testing causes system crashes."
    )
    def test_resource_limits(self, engine_class: type) -> None:
        """Test resource limit configuration."""
        pass


class TestStarlarkEngineSpecific:
    """Tests specific to Starlark engine behavior."""

    @pytest.mark.asyncio
    async def test_starlark_dialect_features(self) -> None:
        """Test Starlark-specific dialect features like f-strings and lambda."""
        engine = StarlarkEngine()

        result = await engine.evaluate_async('name = "World"\nf"Hello, {name}!"')
        assert result == "Hello, World!"


class TestMontyEngineSpecific:
    """Tests specific to Monty engine behavior."""

    @pytest.mark.asyncio
    async def test_try_except(self) -> None:
        """Test that Monty supports try/except (which Starlark doesn't)."""
        engine = MontyEngine()

        script = """
try:
    result = 1 / 0
except ZeroDivisionError:
    result = "caught"
result
"""
        result = await engine.evaluate_async(script)
        assert result == "caught"

    @pytest.mark.asyncio
    async def test_f_strings(self) -> None:
        """Test that Monty supports f-strings natively."""
        engine = MontyEngine()

        result = await engine.evaluate_async('name = "World"\nf"Hello, {name}!"')
        assert result == "Hello, World!"
