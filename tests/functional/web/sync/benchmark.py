"""Recording and summarising time-to-consistency samples.

A sample is how long a client took to show a change after the moment it
became possible to show it: the commit, or the end of an outage that hid the
commit. A sample that never arrived within the scenario's deadline is kept as
a timeout rather than dropped, because "never caught up" is the result the
benchmark exists to expose. Traffic columns are totals across a scenario's
runs, excluding the page load that precedes them.
"""

import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from tests.netem.proxy import ProxyStats

FULL_RUN_ENV = "SYNC_BENCHMARK"
RUNS_ENV = "SYNC_BENCHMARK_RUNS"
OUTPUT_ENV = "SYNC_BENCHMARK_OUTPUT"


def full_run_requested() -> bool:
    return os.environ.get(FULL_RUN_ENV, "") not in {"", "0", "false"}


def runs_per_scenario() -> int:
    if not full_run_requested():
        return 1
    return int(os.environ.get(RUNS_ENV, "5"))


@dataclass
class ScenarioResult:
    client: str
    scenario: str
    profile: str
    samples_s: list[float] = field(default_factory=list)
    timeouts: int = 0
    deadline_s: float = 0.0
    requests_opened: int = 0
    bytes_to_client: int = 0
    bytes_to_server: int = 0

    def add(self, seconds: float | None) -> None:
        if seconds is None:
            self.timeouts += 1
        else:
            self.samples_s.append(seconds)

    def record_traffic(self, stats: ProxyStats, *, since: ProxyStats) -> None:
        """Traffic during the measured runs only, not the page load before them."""
        self.requests_opened = stats.connections_opened - since.connections_opened
        self.bytes_to_client = stats.bytes_to_client - since.bytes_to_client
        self.bytes_to_server = stats.bytes_to_server - since.bytes_to_server

    def percentile(self, q: float) -> float | None:
        """Percentile over all runs, counting a timeout as infinitely slow."""
        total = len(self.samples_s) + self.timeouts
        if total == 0:
            return None
        ordered = sorted(self.samples_s) + [math.inf] * self.timeouts
        index = min(total - 1, math.ceil(q * total) - 1)
        return ordered[max(index, 0)]


def _format_seconds(value: float | None) -> str:
    if value is None:
        return "–"
    if math.isinf(value):
        return "timeout"
    return f"{value:.2f}"


def render_markdown(results: list[ScenarioResult]) -> str:
    lines = [
        "| Client | Scenario | Profile | Runs | p50 (s) | p95 (s) | Timeouts | Connections | KB down |",
        "| ------ | -------- | ------- | ---- | ------- | ------- | -------- | ----------- | ------- |",
    ]
    for result in sorted(results, key=lambda r: (r.client, r.scenario, r.profile)):
        runs = len(result.samples_s) + result.timeouts
        lines.append(
            f"| {result.client} | {result.scenario} | {result.profile} | {runs} "
            f"| {_format_seconds(result.percentile(0.5))} "
            f"| {_format_seconds(result.percentile(0.95))} "
            f"| {result.timeouts} | {result.requests_opened} "
            f"| {result.bytes_to_client / 1024:.0f} |"
        )
    return "\n".join(lines)


def write_report(results: list[ScenarioResult], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = Path(os.environ.get(OUTPUT_ENV, directory / f"sync-benchmark-{stamp}.json"))
    payload = {
        "generated_at": stamp,
        "results": [asdict(r) for r in results],
        "markdown": render_markdown(results),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
