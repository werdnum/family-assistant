"""Tests for the GNU Parallel memory gate used by the adaptive pytest runner."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

# Captured from a 16 GB macOS VM (1,048,576 pages of 16 KiB).
VM_STAT_OUTPUT = """\
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                              295688.
Pages active:                            210566.
Pages inactive:                          296153.
Pages speculative:                        35065.
Pages throttled:                              0.
Pages wired down:                        139765.
Pages purgeable:                            799.
"Translation faults":               39585012675.
Pages copy-on-write:                  602774435.
Pages zero filled:                   1250134838.
Pages reactivated:                  10560336165.
Pages purged:                          86988230.
File-backed pages:                       189931.
Anonymous pages:                         351853.
Pages stored in compressor:              839121.
Pages occupied by compressor:             70349.
Decompressions:                     33189440773.
Compressions:                       33932718123.
"""
TOTAL_BYTES = 17179869184


@pytest.fixture(autouse=True)
def _no_inherited_gate_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run that is logging gate decisions must not collect these tests' ones."""
    monkeypatch.delenv("PYTEST_ADAPTIVE_GATE_LOG", raising=False)


def _load_gate() -> ModuleType:
    script_path = Path(__file__).parents[2] / "scripts" / "memory_gate.py"
    spec = importlib.util.spec_from_file_location("memory_gate", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {script_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_vm_stat_ratio_counts_app_wired_and_compressed_memory() -> None:
    gate = _load_gate()

    ratio = gate.vm_stat_usage_ratio(VM_STAT_OUTPUT, TOTAL_BYTES)

    used_pages = 351853 - 799 + 139765 + 70349
    assert ratio == pytest.approx(used_pages / 1048576)


def test_vm_stat_ratio_rejects_output_missing_a_field() -> None:
    gate = _load_gate()
    output = VM_STAT_OUTPUT.replace("Pages occupied by compressor", "Pages renamed")

    with pytest.raises(ValueError, match="Pages occupied by compressor"):
        gate.vm_stat_usage_ratio(output, TOTAL_BYTES)


def test_cgroup_ratio_reads_current_over_max(tmp_path: Path) -> None:
    gate = _load_gate()
    (tmp_path / "memory.current").write_text("750\n")
    (tmp_path / "memory.max").write_text("1000\n")

    assert gate.cgroup_usage_ratio(tmp_path) == pytest.approx(0.75)


def test_cgroup_without_a_limit_is_not_a_reading(tmp_path: Path) -> None:
    gate = _load_gate()
    (tmp_path / "memory.current").write_text("750\n")
    (tmp_path / "memory.max").write_text("max\n")

    assert gate.cgroup_usage_ratio(tmp_path) is None


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["memory_gate.py"], 0),
        (["memory_gate.py", "0.80"], 1),
    ],
    ids=["platform-default-admits", "explicit-threshold-holds-back"],
)
def test_threshold_defaults_to_the_reading_source(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: int
) -> None:
    gate = _load_gate()
    monkeypatch.setattr(gate, "memory_reading", lambda: gate.MemoryReading(0.85, 0.90))
    monkeypatch.setattr(gate, "shard_is_running", lambda: True)
    monkeypatch.setattr(sys, "argv", argv)

    assert gate.main() == expected


PAGE_FRACTION = 16384 / TOTAL_BYTES


def test_compressor_traffic_is_memory_moved_per_second_since_the_last_sample() -> None:
    gate = _load_gate()
    previous = gate.CompressorSample(taken_at=100.0, moved_pages=1_000_000, traffic=0.0)

    sample = gate.next_compressor_sample(previous, 104.0, 1_052_000, PAGE_FRACTION)

    assert sample.traffic == pytest.approx(52_000 * PAGE_FRACTION / 4.0)
    assert (sample.taken_at, sample.moved_pages) == (104.0, 1_052_000)


def test_a_sample_younger_than_the_window_is_kept_with_its_rate() -> None:
    """Calls arrive faster than a rate can be measured; they reuse the last one."""
    gate = _load_gate()
    previous = gate.CompressorSample(
        taken_at=100.0, moved_pages=1_000_000, traffic=0.04
    )

    sample = gate.next_compressor_sample(previous, 100.3, 1_900_000, PAGE_FRACTION)

    assert sample is previous


@pytest.mark.parametrize(
    ("previous", "moved_pages"),
    [
        (None, 1_000_000),
        ((100.0, 1_000_000, 0.04), 2_000_000),
        ((1_000.0, 9_000_000, 0.04), 1_000_000),
    ],
    ids=["first-call", "stale-sample", "counters-went-backwards"],
)
def test_a_sample_that_cannot_give_a_rate_starts_a_new_window(
    previous: tuple[float, int, float] | None, moved_pages: int
) -> None:
    gate = _load_gate()
    earlier = None if previous is None else gate.CompressorSample(*previous)

    sample = gate.next_compressor_sample(earlier, 1_010.0, moved_pages, PAGE_FRACTION)

    assert sample == gate.CompressorSample(1_010.0, moved_pages, 0.0)


def test_heavy_compressor_traffic_holds_a_job_back_below_the_usage_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thrashing Mac levels off under the usage threshold; traffic is what shows it."""
    gate = _load_gate()
    monkeypatch.setattr(
        gate,
        "memory_reading",
        lambda: gate.MemoryReading(0.87, 0.90, compressor_traffic=0.05),
    )
    monkeypatch.setattr(gate, "shard_is_running", lambda: True)
    monkeypatch.setattr(sys, "argv", ["memory_gate.py"])

    assert gate.main() == 1


def test_pressure_with_no_shard_running_still_admits_a_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise pressure from elsewhere on the machine would hang the run for good."""
    gate = _load_gate()
    monkeypatch.setattr(
        gate,
        "memory_reading",
        lambda: gate.MemoryReading(0.97, 0.90, compressor_traffic=0.05),
    )
    monkeypatch.setattr(gate, "shard_is_running", lambda: False)
    monkeypatch.setattr(sys, "argv", ["memory_gate.py"])

    assert gate.main() == 0


def test_running_shard_pattern_matches_a_shard_and_not_its_scheduler() -> None:
    gate = _load_gate()
    shard = "python '/repo/scripts/run_pytest_shard.py' --adaptive-manifest '/repo/.pytest-adaptive/shards/shard-9.json'"
    scheduler = "perl parallel --jobs 10 python /repo/scripts/run_pytest_shard.py --adaptive-manifest {} :::: /repo/.pytest-adaptive/shards.txt"

    assert re.search(gate._RUNNING_SHARD_PATTERN, shard)
    assert not re.search(gate._RUNNING_SHARD_PATTERN, scheduler)


def test_unmeasurable_memory_admits_the_job_and_says_why(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """GNU Parallel retries a held-back job forever, so failing closed would hang."""
    gate = _load_gate()

    def fail() -> None:
        raise subprocess.CalledProcessError(1, ["vm_stat"])

    monkeypatch.setattr(gate, "memory_reading", fail)
    monkeypatch.setattr(sys, "argv", ["memory_gate.py"])

    assert gate.main() == 0
    assert "cannot measure memory" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform != "darwin", reason="reads this Mac's vm_stat")
def test_macos_reading_is_a_plausible_fraction_of_memory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = _load_gate()
    monkeypatch.setenv("PYTEST_ADAPTIVE_GATE_STATE", str(tmp_path / "state.json"))

    reading = gate.macos_reading()

    assert 0.0 < reading.ratio < 1.0
    assert reading.compressor_traffic >= 0.0
