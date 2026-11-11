#!/usr/bin/env python3
"""GNU Parallel --limit helper that holds back new jobs under memory pressure.

Usage is measured from cgroup v2 where one sets a limit (CI and containers), or
on macOS from ``vm_stat``. Each source has its own default threshold because
the two ratios do not mean the same thing; an explicit threshold argument
overrides both.

The macOS ratio is Activity Monitor's "Memory Used" over physical memory: app
memory, wired memory and the compressor's footprint. File cache is left out,
because the system drops it on demand. In a 9-CPU, 16 GB VM a healthy full test
run at 16 jobs peaked at 0.85, hence 0.90.

That ratio alone does not see a Mac thrash. Compression shrinks the very pages
it counts, so it levels off below the threshold while the machine spends its
time moving pages in and out of the compressor; the kernel's own pressure level
takes minutes to change. Compressor traffic is the prompt signal, so macOS also
holds a job back while that traffic is heavy. In the same VM a healthy 10-job
run moved a median of 0.03% of physical memory through the compressor per
second and stayed under 3% for 98% of it; a run that thrashed, with 34 browser
test reruns, was over 4.6% for 90% of it.

A gate that only ever says "wait" can hang a run for good when the pressure
comes from something else on the machine, so a job is always admitted when no
shard is running.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

CGROUP_DEFAULT_THRESHOLD = 0.80
MACOS_DEFAULT_THRESHOLD = 0.90
# Fraction of physical memory compressed plus decompressed per second.
MACOS_COMPRESSOR_TRAFFIC_THRESHOLD = 0.03
# GNU Parallel asks the gate about once a second for the whole run, so the gate
# cannot stop to measure a rate. Each call compares the counters with the ones
# an earlier call left behind, once they are at least this far apart.
COMPRESSOR_WINDOW_SECONDS = 2.0
COMPRESSOR_SAMPLE_MAX_AGE_SECONDS = 30.0

_VM_STAT_PAGE_SIZE = re.compile(r"page size of (\d+) bytes")
_VM_STAT_USAGE_FIELDS = {
    "anonymous": "Anonymous pages",
    "purgeable": "Pages purgeable",
    "wired": "Pages wired down",
    "compressor": "Pages occupied by compressor",
}
_VM_STAT_TRAFFIC_FIELDS = {
    "compressions": "Compressions",
    "decompressions": "Decompressions",
}
# A shard's command line names its manifest; GNU Parallel's own names a
# placeholder, so this matches running shards and not their scheduler.
_RUNNING_SHARD_PATTERN = r"run_pytest_shard\.py.*shard-[0-9]+\.json"


class CompressorSample(NamedTuple):
    """Cumulative compressor counters at a moment, and the rate they then showed."""

    taken_at: float
    moved_pages: int
    traffic: float


class MemoryReading(NamedTuple):
    """A usage ratio, the threshold that suits its source, and compressor traffic."""

    ratio: float
    default_threshold: float
    compressor_traffic: float = 0.0


def cgroup_usage_ratio(cgroup_path: Path = Path("/sys/fs/cgroup")) -> float | None:
    """Return current/max cgroup memory usage, or None when no max is visible."""
    try:
        current = int((cgroup_path / "memory.current").read_text())
        maximum_text = (cgroup_path / "memory.max").read_text().strip()
    except FileNotFoundError:
        return None

    if maximum_text == "max":
        return None

    maximum = int(maximum_text)
    if maximum <= 0:
        return None

    return current / maximum


def _vm_stat_pages(vm_stat_output: str, fields: dict[str, str]) -> dict[str, int]:
    pages: dict[str, int] = {}
    for key, label in fields.items():
        match = re.search(rf"^{re.escape(label)}:\s+(\d+)\.", vm_stat_output, re.M)
        if match is None:
            raise ValueError(f"vm_stat output has no '{label}' line")
        pages[key] = int(match.group(1))
    return pages


def _vm_stat_page_size(vm_stat_output: str) -> int:
    page_size_match = _VM_STAT_PAGE_SIZE.search(vm_stat_output)
    if page_size_match is None:
        raise ValueError("vm_stat output does not state its page size")
    return int(page_size_match.group(1))


def vm_stat_usage_ratio(vm_stat_output: str, total_bytes: int) -> float:
    """Return macOS "Memory Used" over physical memory from ``vm_stat`` output."""
    pages = _vm_stat_pages(vm_stat_output, _VM_STAT_USAGE_FIELDS)
    used_pages = (
        pages["anonymous"] - pages["purgeable"] + pages["wired"] + pages["compressor"]
    )
    return used_pages * _vm_stat_page_size(vm_stat_output) / total_bytes


def next_compressor_sample(
    previous: CompressorSample | None,
    taken_at: float,
    moved_pages: int,
    memory_fraction_per_page: float,
) -> CompressorSample:
    """Return the sample to remember, carrying the traffic rate it implies.

    A previous sample younger than the window is kept as it is, so the rate is
    always measured over a meaningful interval. One that is missing, stale, or
    ahead of the counters (a reboot) starts a new window with no rate yet.
    """
    if previous is None or moved_pages < previous.moved_pages:
        return CompressorSample(taken_at, moved_pages, 0.0)
    age = taken_at - previous.taken_at
    if age > COMPRESSOR_SAMPLE_MAX_AGE_SECONDS:
        return CompressorSample(taken_at, moved_pages, 0.0)
    if age < COMPRESSOR_WINDOW_SECONDS:
        return previous
    traffic = (moved_pages - previous.moved_pages) * memory_fraction_per_page / age
    return CompressorSample(taken_at, moved_pages, traffic)


def _compressor_state_path() -> Path:
    configured = os.environ.get("PYTEST_ADAPTIVE_GATE_STATE")
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / f"fa-memory-gate-{os.getuid()}.json"


def _load_compressor_sample(path: Path) -> CompressorSample | None:
    try:
        taken_at, moved_pages, traffic = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    return CompressorSample(float(taken_at), int(moved_pages), float(traffic))


def _store_compressor_sample(path: Path, sample: CompressorSample) -> None:
    scratch = path.with_name(f"{path.name}.{os.getpid()}")
    scratch.write_text(json.dumps(sample), encoding="utf-8")
    scratch.replace(path)


def macos_reading() -> MemoryReading:
    """Measure the running Mac's memory usage and compressor traffic."""
    total_bytes = int(
        subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, check=True
        ).stdout
    )
    vm_stat_output = subprocess.run(
        ["vm_stat"], capture_output=True, text=True, check=True
    ).stdout
    moved = _vm_stat_pages(vm_stat_output, _VM_STAT_TRAFFIC_FIELDS)

    state_path = _compressor_state_path()
    previous = _load_compressor_sample(state_path)
    sample = next_compressor_sample(
        previous,
        time.time(),
        sum(moved.values()),
        _vm_stat_page_size(vm_stat_output) / total_bytes,
    )
    if sample is not previous:
        _store_compressor_sample(state_path, sample)

    return MemoryReading(
        ratio=vm_stat_usage_ratio(vm_stat_output, total_bytes),
        default_threshold=MACOS_DEFAULT_THRESHOLD,
        compressor_traffic=sample.traffic,
    )


def shard_is_running() -> bool:
    """Whether any adaptive pytest shard is running on this machine."""
    return (
        subprocess.run(
            ["pgrep", "-f", _RUNNING_SHARD_PATTERN], capture_output=True, check=False
        ).returncode
        == 0
    )


def memory_reading() -> MemoryReading | None:
    """Return this machine's memory usage, or None when nothing measures it."""
    ratio = cgroup_usage_ratio()
    if ratio is not None:
        return MemoryReading(ratio, CGROUP_DEFAULT_THRESHOLD)
    if sys.platform == "darwin":
        return macos_reading()
    return None


def _log_decision(reading: MemoryReading, decision: int) -> None:
    """Append this decision to PYTEST_ADAPTIVE_GATE_LOG, for tuning the gate."""
    log_path = os.environ.get("PYTEST_ADAPTIVE_GATE_LOG")
    if not log_path:
        return
    with Path(log_path).open("a", encoding="utf-8") as log:
        log.write(
            f"{time.time():.1f}\t{reading.ratio:.3f}\t"
            f"{reading.compressor_traffic:.4f}\t{decision}\n"
        )


def _decide(argv: list[str]) -> int:
    reading = memory_reading()
    if reading is None:
        return 0
    threshold = float(argv[1]) if len(argv) > 1 else reading.default_threshold
    under_pressure = (
        reading.ratio >= threshold
        or reading.compressor_traffic >= MACOS_COMPRESSOR_TRAFFIC_THRESHOLD
    )
    decision = 1 if under_pressure and shard_is_running() else 0
    _log_decision(reading, decision)
    return decision


def main() -> int:
    """Exit using GNU Parallel --limit semantics: 0 starts a job, 1 waits."""
    try:
        return _decide(sys.argv)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        # GNU Parallel retries a waiting job forever, so failing closed would
        # hang the run without a word; say why and let the job start.
        print(
            f"memory_gate: cannot measure memory, not gating: {error}", file=sys.stderr
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
