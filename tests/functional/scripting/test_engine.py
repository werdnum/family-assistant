"""
Tests for the scripting engine integration.

This module tests the MontyEngine integration, validating that it implements
the scripting interface correctly.
"""

import asyncio
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.scripting.config import ScriptConfig
from family_assistant.scripting.errors import (
    ScriptExecutionError,
    ScriptSyntaxError,
    ScriptTimeoutError,
)
from family_assistant.scripting.monty_engine import MontyEngine, ScriptOutputBuffer
from family_assistant.storage.database import Database
from family_assistant.tools.types import ToolExecutionContext


class TestEngineIntegration:
    """Test engine integration with MontyEngine."""

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

        assert "unexpected EOF" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_runtime_error_handling(self, engine_class: type) -> None:
        """Test that runtime errors are properly converted to ScriptExecutionError."""
        engine = engine_class()

        with pytest.raises(ScriptExecutionError):
            await engine.evaluate_async("1 / 0")

        with pytest.raises(ScriptExecutionError):
            await engine.evaluate_async("undefined_variable")

    @pytest.mark.asyncio
    async def test_captured_output_collects_print(self, engine_class: type) -> None:
        """print() output is captured into the caller-supplied output buffer."""
        engine = engine_class()

        script = """
print("hello")
print("world")
42
"""
        buffer = ScriptOutputBuffer()
        result = await engine.evaluate_async(script, output_buffer=buffer)

        assert result == 42
        assert buffer.getvalue() == "hello\nworld\n"

    @pytest.mark.asyncio
    async def test_captured_output_empty_without_print(
        self, engine_class: type
    ) -> None:
        """The output buffer stays empty when the script prints nothing."""
        engine = engine_class()

        buffer = ScriptOutputBuffer()
        await engine.evaluate_async("1 + 1", output_buffer=buffer)

        assert not buffer.getvalue()

    @pytest.mark.asyncio
    async def test_captured_output_isolated_across_concurrent_runs(
        self, engine_class: type
    ) -> None:
        """Concurrent runs on one engine capture into their own buffers only."""
        engine = engine_class()

        buffer_a = ScriptOutputBuffer()
        buffer_b = ScriptOutputBuffer()

        results = await asyncio.gather(
            engine.evaluate_async('print("from a")\n1', output_buffer=buffer_a),
            engine.evaluate_async('print("from b")\n2', output_buffer=buffer_b),
        )

        assert results == [1, 2]
        assert "from a" in buffer_a.getvalue()
        assert "from b" not in buffer_a.getvalue()
        assert "from b" in buffer_b.getvalue()
        assert "from a" not in buffer_b.getvalue()

    @pytest.mark.asyncio
    async def test_captured_output_available_after_failure(
        self, engine_class: type
    ) -> None:
        """Output printed before a runtime failure is still captured."""
        engine = engine_class()

        script = """
print("before failure")
1 / 0
"""
        buffer = ScriptOutputBuffer()
        with pytest.raises(ScriptExecutionError):
            await engine.evaluate_async(script, output_buffer=buffer)

        assert "before failure" in buffer.getvalue()

    @pytest.mark.asyncio
    async def test_captured_output_is_bounded(self, engine_class: type) -> None:
        """A chatty script cannot grow the buffer past its character budget."""
        engine = engine_class()

        # Print far more than the cap so truncation must kick in.
        script = """
i = 0
while i < 5000:
    print("0123456789")
    i = i + 1
0
"""
        buffer = ScriptOutputBuffer(max_chars=1024)
        await engine.evaluate_async(script, output_buffer=buffer)

        output = buffer.getvalue()
        assert buffer.truncated
        assert "... [output truncated] ..." in output
        # Retained output stays close to the cap (marker adds a little).
        assert len(output) < 1024 + 64

    @pytest.mark.asyncio
    async def test_script_exceeding_execution_time_times_out(
        self, engine_class: type
    ) -> None:
        """A script running past max_execution_time raises ScriptTimeoutError."""
        engine = engine_class(config=ScriptConfig(max_execution_time=0.3))

        with pytest.raises(ScriptTimeoutError) as exc_info:
            await engine.evaluate_async("while True:\n    pass")

        assert exc_info.value.timeout_seconds == 0.3

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

        # ast-grep-ignore: no-dict-any - function returns dynamic data for scripting engine test
        def get_data() -> dict[str, Any]:
            return {"status": "ok", "count": 3}

        globals_dict["get_data"] = get_data
        assert await engine.evaluate_async("get_data()['status']", globals_dict) == "ok"
        assert await engine.evaluate_async("get_data()['count']", globals_dict) == 3

    @pytest.mark.parametrize(
        ("script", "expected_error"),
        [
            pytest.param(
                "def f(n):\n    return f(n + 1)\nf(0)",
                "RecursionError",
                id="unbounded-recursion",
            ),
            pytest.param(
                'x = "a" * (512 * 1024 * 1024)\nlen(x)',
                "MemoryError",
                id="oversized-allocation",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_resource_limits_stop_runaway_scripts(
        self, engine_class: type, script: str, expected_error: str
    ) -> None:
        """Runaway recursion and oversized allocations fail the script cleanly."""
        engine = engine_class()

        with pytest.raises(ScriptExecutionError, match=expected_error):
            await engine.evaluate_async(script)


class TestMontyEngineSpecific:
    """Tests specific to Monty engine behavior."""

    @pytest.mark.asyncio
    async def test_try_except(self) -> None:
        """Test that Monty supports try/except."""
        engine = MontyEngine(default_timezone=ZoneInfo("Australia/Sydney"))

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
        engine = MontyEngine(default_timezone=ZoneInfo("Australia/Sydney"))

        result = await engine.evaluate_async('name = "World"\nf"Hello, {name}!"')
        assert result == "Hello, World!"

    @pytest.mark.asyncio
    async def test_async_function_called_without_await(self) -> None:
        """Async host functions are awaited on the host, so scripts call them plainly."""
        engine = MontyEngine(default_timezone=ZoneInfo("Australia/Sydney"))

        async def fetch(key: str) -> dict[str, str]:
            return {"key": key}

        result = await engine.evaluate_async('fetch("a")["key"]', {"fetch": fetch})
        assert result == "a"

    @pytest.mark.asyncio
    async def test_many_host_calls(self) -> None:
        """A script may make more host calls than Monty's default cap of 1000."""
        engine = MontyEngine(default_timezone=ZoneInfo("Australia/Sydney"))

        def one() -> int:
            return 1

        result = await engine.evaluate_async(
            "total = 0\nfor _ in range(1500):\n    total += one()\ntotal",
            {"one": one},
        )
        assert result == 1500

    @pytest.mark.asyncio
    async def test_naive_now_uses_household_timezone(self) -> None:
        engine = MontyEngine(default_timezone=ZoneInfo("Asia/Kolkata"))

        script = """
from datetime import datetime
datetime.now().astimezone().utcoffset().total_seconds()
"""
        result = await engine.evaluate_async(script)
        assert result == 5.5 * 3600

    @pytest.mark.asyncio
    async def test_naive_now_prefers_call_timezone(
        self, db_engine: AsyncEngine
    ) -> None:
        engine = MontyEngine(default_timezone=ZoneInfo("UTC"))
        context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-conv",
            user_name="test",
            turn_id=None,
            db_context=Database(engine=db_engine),
            processing_service=None,
            clock=None,
            plugins=None,
            event_sources=None,
            attachment_registry=None,
            timezone=ZoneInfo("Asia/Kolkata"),
            credential_resolvers=None,
            api_backend=None,
        )

        script = """
from datetime import datetime
datetime.now().astimezone().utcoffset().total_seconds()
"""
        result = await engine.evaluate_async(script, execution_context=context)
        assert result == 5.5 * 3600

    @pytest.mark.asyncio
    async def test_no_double_resume_when_script_fails_after_external_call(
        self,
    ) -> None:
        """Regression: each snapshot must resume exactly once.

        The bug had both the function call and progress.resume() in the same
        try block. When resume(return_value=result) raised because the script
        failed after the call returned, the except handler resumed the same
        snapshot a second time, and its "Progress already resumed" error
        replaced the script's own error.
        """
        engine = MontyEngine(default_timezone=ZoneInfo("Australia/Sydney"))
        call_count = 0

        def returns_zero() -> int:
            nonlocal call_count
            call_count += 1
            return 0

        with pytest.raises(ScriptExecutionError, match="ZeroDivisionError"):
            await engine.evaluate_async(
                "1 / returns_zero()", {"returns_zero": returns_zero}
            )

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_exception_in_function_does_not_corrupt_state(self) -> None:
        """Test that script state remains consistent after function exceptions."""
        engine = MontyEngine(default_timezone=ZoneInfo("Australia/Sydney"))
        call_count = 0

        def flaky_fn() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient error")
            return "success"

        script = """
results = []
try:
    results.append(flaky_fn())
except RuntimeError:
    results.append("error")
results.append(flaky_fn())
results
"""
        result = await engine.evaluate_async(script, {"flaky_fn": flaky_fn})
        assert result == ["error", "success"]
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_async_function_exception_single_resume(self) -> None:
        """Test that async external functions that raise also get a single resume."""
        engine = MontyEngine(default_timezone=ZoneInfo("Australia/Sydney"))
        call_count = 0

        async def async_failing_fn() -> None:
            nonlocal call_count
            call_count += 1
            raise ValueError("async error")

        script = """
try:
    async_failing_fn()
    result = "should not reach"
except ValueError:
    result = "async caught"
result
"""
        result = await engine.evaluate_async(
            script, {"async_failing_fn": async_failing_fn}
        )
        assert result == "async caught"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_async_function_exception_state_consistency(self) -> None:
        """Test state consistency with async functions that fail then succeed."""
        engine = MontyEngine(default_timezone=ZoneInfo("Australia/Sydney"))
        call_count = 0

        async def async_flaky_fn() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("async transient error")
            return "async success"

        script = """
results = []
try:
    results.append(async_flaky_fn())
except RuntimeError:
    results.append("error")
results.append(async_flaky_fn())
results
"""
        result = await engine.evaluate_async(script, {"async_flaky_fn": async_flaky_fn})
        assert result == ["error", "async success"]
        assert call_count == 2


class TestScriptOutputBuffer:
    """Unit tests for ScriptOutputBuffer bounding behaviour."""

    def test_skips_empty_chunks(self) -> None:
        """A flood of empty chunks never grows the buffer or trips the cap."""
        buffer = ScriptOutputBuffer(max_chars=100)

        for _ in range(10_000):
            buffer.append("")

        assert not buffer.truncated
        assert not buffer.getvalue()

        buffer.append("hello")
        assert buffer.getvalue() == "hello"

    def test_bounds_single_huge_chunk(self) -> None:
        """A single oversized chunk is clipped to the cap, not retained whole."""
        buffer = ScriptOutputBuffer(max_chars=100)

        buffer.append("x" * 1_000_000)

        assert buffer.truncated
        output = buffer.getvalue()
        assert "... [output truncated] ..." in output
        # Only a budget-sized prefix is retained, never the full megabyte.
        assert output.startswith("x" * 100)
        assert len(output) < 100 + 64

    def test_accumulates_until_cap(self) -> None:
        """Chunks accumulate in order until the character budget is exceeded."""
        buffer = ScriptOutputBuffer(max_chars=10)

        buffer.append("abc")
        buffer.append("def")
        assert not buffer.truncated
        assert buffer.getvalue() == "abcdef"

        buffer.append("ghijkl")  # pushes past the 10-char cap
        assert buffer.truncated
        assert buffer.getvalue().startswith("abcdefghij")
        assert "... [output truncated] ..." in buffer.getvalue()
