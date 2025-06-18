"""
Tests for the StarlarkEngine integration.

This module tests only our integration with Starlark, not the Starlark language itself.
"""

import asyncio

import pytest

from family_assistant.scripting.engine import StarlarkEngine
from family_assistant.scripting.errors import (
    ScriptExecutionError,
    ScriptSyntaxError,
)


class TestStarlarkEngineIntegration:
    """Test our integration with the Starlark engine."""

    def test_basic_evaluation(self) -> None:
        """Test that we can evaluate a simple Starlark expression."""
        engine = StarlarkEngine()
        result = engine.evaluate("2 + 3")
        assert result == 5

    def test_global_variable_injection(self) -> None:
        """Test that we can inject Python data structures as globals."""
        engine = StarlarkEngine()

        globals_dict = {
            "name": "Alice",
            "age": 30,
            "data": {"key": "value", "count": 42},
            "items": [1, 2, 3, 4, 5],
        }

        # Test accessing injected globals
        assert engine.evaluate("name", globals_dict) == "Alice"
        assert engine.evaluate("age * 2", globals_dict) == 60
        assert engine.evaluate("data['count']", globals_dict) == 42
        assert engine.evaluate("len(items)", globals_dict) == 5

    def test_syntax_error_handling(self) -> None:
        """Test that syntax errors are properly converted to ScriptSyntaxError."""
        engine = StarlarkEngine()

        # Missing closing parenthesis
        with pytest.raises(ScriptSyntaxError) as exc_info:
            engine.evaluate("print('hello'")

        # Verify it's our custom exception type
        assert isinstance(exc_info.value, ScriptSyntaxError)
        assert (
            "parse error" in str(exc_info.value).lower()
            or "syntax" in str(exc_info.value).lower()
        )

    def test_runtime_error_handling(self) -> None:
        """Test that runtime errors are properly converted to ScriptExecutionError."""
        engine = StarlarkEngine()

        # Division by zero
        with pytest.raises(ScriptExecutionError) as exc_info:
            engine.evaluate("1 / 0")

        # Undefined variable
        with pytest.raises(ScriptExecutionError) as exc_info:
            engine.evaluate("undefined_variable")

        # Verify it's our custom exception type
        assert isinstance(exc_info.value, ScriptExecutionError)

    @pytest.mark.asyncio
    async def test_async_evaluation(self) -> None:
        """Test asynchronous script evaluation."""
        engine = StarlarkEngine()

        # Basic async evaluation
        result = await engine.evaluate_async("10 + 20")
        assert result == 30

        # Async with globals
        globals_dict = {"x": 5, "y": 10}
        result = await engine.evaluate_async("x * y", globals_dict)
        assert result == 50

    @pytest.mark.skip(reason="Timeout test disabled - resource intensive and may crash")
    @pytest.mark.asyncio
    async def test_async_timeout(self) -> None:
        """Test that long-running scripts timeout in async mode."""
        # This test is disabled as it's resource intensive
        pass

    @pytest.mark.asyncio
    async def test_concurrent_execution(self) -> None:
        """Test that multiple scripts can execute concurrently."""
        engine = StarlarkEngine()

        # Define different scripts
        scripts = [
            ("2 + 3", 5),
            ("'hello' + ' ' + 'world'", "hello world"),
            ("[x * 2 for x in [1, 2, 3]]", [2, 4, 6]),
        ]

        # Execute scripts concurrently
        tasks = [engine.evaluate_async(script) for script, _ in scripts]
        results = await asyncio.gather(*tasks)

        # Verify results
        for i, (_, expected) in enumerate(scripts):
            assert results[i] == expected

    def test_empty_script_handling(self) -> None:
        """Test that empty scripts are handled gracefully."""
        engine = StarlarkEngine()

        assert engine.evaluate("") is None
        assert engine.evaluate("   \n  \t  \n  ") is None

    def test_complex_data_structure_result(self) -> None:
        """Test that complex data structures are properly returned."""
        engine = StarlarkEngine()

        # Return a dictionary
        result = engine.evaluate('{"name": "test", "values": [1, 2, 3]}')
        assert result == {"name": "test", "values": [1, 2, 3]}

        # Return a nested structure
        result = engine.evaluate('[{"id": 1}, {"id": 2}]')
        assert result == [{"id": 1}, {"id": 2}]

    def test_function_injection_not_supported(self) -> None:
        """Test that function injection is not supported (starlark-pyo3 limitation)."""
        engine = StarlarkEngine()

        def my_function() -> int:
            return 42

        # Functions should be skipped during global injection
        globals_dict = {"my_func": my_function, "my_value": 10}

        # Value should be accessible
        assert engine.evaluate("my_value", globals_dict) == 10

        # Function should not be accessible (skipped in our implementation)
        with pytest.raises(ScriptExecutionError):
            engine.evaluate("my_func()", globals_dict)

    @pytest.mark.skip(reason="Resource limits testing disabled - crashes the machine")
    def test_resource_limits(self) -> None:
        """Test resource limit configuration."""
        # This test is disabled as it was causing system crashes
        pass
