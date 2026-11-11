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

import re
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

CGROUP_DEFAULT_THRESHOLD = 0.80
MACOS_DEFAULT_THRESHOLD = 0.90
# Fraction of physical memory compressed plus decompressed per second.
MACOS_COMPRESSOR_TRAFFIC_THRESHOLD = 0.03
COMPRESSOR_SAMPLE_SECONDS = 1.0

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


def vm_stat_compressor_traffic(
    earlier_output: str, later_output: str, seconds: float, total_bytes: int
) -> float:
    """Return the fraction of physical memory (de)compressed per second."""
    earlier = _vm_stat_pages(earlier_output, _VM_STAT_TRAFFIC_FIELDS)
    later = _vm_stat_pages(later_output, _VM_STAT_TRAFFIC_FIELDS)
    moved_pages = sum(later[key] - earlier[key] for key in _VM_STAT_TRAFFIC_FIELDS)
    return moved_pages * _vm_stat_page_size(later_output) / total_bytes / seconds


def _vm_stat() -> str:
    return subprocess.run(
        ["vm_stat"], capture_output=True, text=True, check=True
    ).stdout


def macos_reading() -> MemoryReading:
    """Measure the running Mac's memory usage and compressor traffic."""
    total_bytes = int(
        subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, check=True
        ).stdout
    )
    earlier_output = _vm_stat()
    started = time.monotonic()
    time.sleep(COMPRESSOR_SAMPLE_SECONDS)
    later_output = _vm_stat()
    return MemoryReading(
        ratio=vm_stat_usage_ratio(later_output, total_bytes),
        default_threshold=MACOS_DEFAULT_THRESHOLD,
        compressor_traffic=vm_stat_compressor_traffic(
            earlier_output, later_output, time.monotonic() - started, total_bytes
        ),
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


def main() -> int:
    """Exit using GNU Parallel --limit semantics: 0 starts a job, 1 waits."""
    try:
        reading = memory_reading()
        if reading is None:
            return 0
        threshold = (
            float(sys.argv[1]) if len(sys.argv) > 1 else reading.default_threshold
        )
        under_pressure = (
            reading.ratio >= threshold
            or reading.compressor_traffic >= MACOS_COMPRESSOR_TRAFFIC_THRESHOLD
        )
        return 1 if under_pressure and shard_is_running() else 0
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        # GNU Parallel retries a waiting job forever, so failing closed would
        # hang the run without a word; say why and let the job start.
        print(
            f"memory_gate: cannot measure memory, not gating: {error}", file=sys.stderr
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
