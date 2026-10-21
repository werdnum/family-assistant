"""Tests for running jq programs in a child process."""

import asyncio
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from family_assistant.tools.data_manipulation import run_jq_in_subprocess

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolResult

# Roughly a minute of CPU work in jq: far longer than any timeout used here.
SLOW_PROGRAM = "reduce range(1000000000) as $i (0; . + $i)"


@pytest.fixture
def json_file(tmp_path: Path) -> Path:
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"items": [{"id": 1}, {"id": 2}]}))
    return path


async def _max_loop_gap_while(awaitable: "asyncio.Task[ToolResult]") -> float:
    """Spin the loop with call_soon until ``awaitable`` finishes; return the worst gap."""
    loop = asyncio.get_running_loop()
    max_gap = 0.0
    last = time.monotonic()

    def tick() -> None:
        nonlocal last, max_gap
        now = time.monotonic()
        max_gap = max(max_gap, now - last)
        last = now
        if not awaitable.done():
            loop.call_soon(tick)

    loop.call_soon(tick)
    await awaitable
    return max_gap


@pytest.mark.asyncio
async def test_query_returns_data_and_text(json_file: Path) -> None:
    result = await run_jq_in_subprocess(".items | map(.id)", json_file)

    assert result.get_data() == [1, 2]
    assert result.get_text() == json.dumps([1, 2], indent=2)


@pytest.mark.asyncio
async def test_multiple_outputs_are_returned_as_list(json_file: Path) -> None:
    result = await run_jq_in_subprocess(".items[].id", json_file)

    assert result.get_data() == [1, 2]


@pytest.mark.asyncio
async def test_slow_program_times_out_without_blocking_loop(json_file: Path) -> None:
    started = time.monotonic()
    task = asyncio.ensure_future(
        run_jq_in_subprocess(SLOW_PROGRAM, json_file, timeout_seconds=1.5)
    )

    max_gap = await _max_loop_gap_while(task)

    result = task.result()
    assert result.text is not None
    assert "timed out" in result.text
    assert time.monotonic() - started < 10
    assert max_gap < 0.5


@pytest.mark.asyncio
async def test_oversized_output_is_rejected(json_file: Path) -> None:
    result = await run_jq_in_subprocess(
        "[range(100000)]", json_file, max_output_bytes=10_000
    )

    assert result.text is not None
    assert "larger than 10000 bytes" in result.text


@pytest.mark.asyncio
async def test_noisy_stderr_does_not_stall(json_file: Path) -> None:
    result = await run_jq_in_subprocess(
        "[range(20000) | debug] | length", json_file, timeout_seconds=20
    )

    assert result.get_data() == 20000


@pytest.mark.asyncio
async def test_invalid_program_reports_jq_error(json_file: Path) -> None:
    result = await run_jq_in_subprocess(".items[", json_file)

    assert result.text is not None
    assert result.text.startswith("Error: Invalid jq query.")
    assert "syntax error" in result.text


@pytest.mark.asyncio
async def test_invalid_json_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json")

    result = await run_jq_in_subprocess(".", path)

    assert result.text is not None
    assert "not valid JSON" in result.text
