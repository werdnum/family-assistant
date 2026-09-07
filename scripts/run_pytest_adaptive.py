#!/usr/bin/env python3
"""Run pytest shards through GNU Parallel with live load and cgroup memory gates."""

from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

PYTEST_EXIT_NO_TESTS_COLLECTED = 5
DEFAULT_COLLECTION_SELECTOR = "tests"
JsonObject = dict[str, object]

OPTION_TAKES_VALUE = {
    "--basetemp",
    "--browser",
    "--db",
    "--ignore",
    "--ignore-glob",
    "--maxfail",
    "--record-mode",
    "--rootdir",
    "--tb",
    "--timeout",
    "-k",
    "-m",
    "-o",
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _pytest_bin() -> str:
    configured = os.environ.get("PYTEST_BIN")
    if configured:
        return configured
    return str(_repo_root() / ".venv" / "bin" / "pytest")


def _parallel_bin() -> str:
    configured = os.environ.get("GNU_PARALLEL")
    if configured:
        return configured

    discovered = shutil.which("parallel")
    if discovered is None:
        raise RuntimeError(
            "GNU Parallel is required for PYTEST_RUNNER=adaptive. "
            "Install 'parallel' or set GNU_PARALLEL=/path/to/parallel."
        )
    return discovered


def _sanitize_pytest_args(args: list[str]) -> list[str]:
    """Remove options that are unsafe or counterproductive inside shards."""
    sanitized: list[str] = []
    skip_next = False

    for arg in args:
        if skip_next:
            skip_next = False
            continue

        sanitized_arg = arg
        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 2:
            compact_flags = arg[1:]
            if set(compact_flags) <= {"q", "x"}:
                compact_flags = compact_flags.replace("q", "")
                if not compact_flags:
                    continue
                sanitized_arg = f"-{compact_flags}"

        if sanitized_arg in {
            "--disable-warnings",
            "--json-report",
            "--json-report-file",
            "--quiet",
            "--tb",
            "-n",
            "-q",
            "--numprocesses",
        }:
            if sanitized_arg in {"--json-report-file", "--tb", "-n", "--numprocesses"}:
                skip_next = True
            continue
        if sanitized_arg.startswith("--json-report-file="):
            continue
        if sanitized_arg.startswith("--tb="):
            continue
        if sanitized_arg.startswith("--numprocesses="):
            continue
        if sanitized_arg.startswith("-n") and sanitized_arg != "-n0":
            continue
        sanitized.append(sanitized_arg)

    return sanitized


def _json_report_file(args: list[str]) -> Path | None:
    skip_next = False

    for index, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if arg == "--json-report-file":
            if index + 1 < len(args):
                return Path(args[index + 1])
            return None
        if arg.startswith("--json-report-file="):
            return Path(arg.removeprefix("--json-report-file="))
        if arg in {"--tb", "-n", "--numprocesses"}:
            skip_next = True

    return None


def _strip_collection_selectors(args: list[str]) -> list[str]:
    """Keep pytest options for shard runs, but drop paths/nodeids already scheduled."""
    shard_args: list[str] = []
    skip_next = False

    for arg in args:
        if skip_next:
            shard_args.append(arg)
            skip_next = False
            continue

        if arg in OPTION_TAKES_VALUE:
            shard_args.append(arg)
            skip_next = True
            continue
        if arg.startswith("-"):
            shard_args.append(arg)

    return shard_args


def _has_collection_selector(args: list[str]) -> bool:
    """Return true when pytest args include an explicit path or nodeid selector."""
    skip_next = False

    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "--":
            return True
        if arg in OPTION_TAKES_VALUE:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        return True

    return False


def _with_default_collection_selector(args: list[str]) -> list[str]:
    """Ensure pytest loads tests/conftest.py when no selectors were provided."""
    if _has_collection_selector(args):
        return args
    return [*args, DEFAULT_COLLECTION_SELECTOR]


def _collect_nodeids(
    pytest_bin: str, pytest_args: list[str], nodeids_file: Path
) -> int:
    collect_report = nodeids_file.with_suffix(".json")
    command = [
        pytest_bin,
        "--collect-only",
        f"--adaptive-nodeids-file={collect_report}",
        "-q",
        "-n0",
        *pytest_args,
    ]
    print("Collecting pytest nodeids for adaptive scheduling...")
    result = subprocess.run(
        command,
        check=False,
        cwd=_repo_root(),
        stderr=subprocess.STDOUT,
        stdout=subprocess.PIPE,
        text=True,
    )

    if result.returncode not in {0, PYTEST_EXIT_NO_TESTS_COLLECTED}:
        print(result.stdout, end="")
        return result.returncode

    collection = json.loads(collect_report.read_text(encoding="utf-8"))
    nodeids = collection["nodeids"]
    nodeids_file.write_text(
        "\n".join(nodeids) + ("\n" if nodeids else ""),
        encoding="utf-8",
    )
    print(f"Collected {len(nodeids)} pytest nodeids.")
    return 0


def _batch_nodeids(
    nodeids: list[str], batch_size: int, playwright_nodeids: set[str] | None = None
) -> list[list[str]]:
    """Amortize backend setup while keeping slow browser work parallelizable."""
    if batch_size < 1:
        raise ValueError("PYTEST_ADAPTIVE_BATCH_SIZE must be positive")
    browser_ids = playwright_nodeids or set()
    browser_nodeids = [nodeid for nodeid in nodeids if nodeid in browser_ids]
    modules: dict[str, list[str]] = {}
    for nodeid in nodeids:
        if nodeid not in browser_ids:
            modules.setdefault(nodeid.split("::", 1)[0], []).append(nodeid)

    # Browser tests are substantially slower per nodeid. Start them first and cap
    # their batches even when a single module exceeds the normal backend target.
    browser_batch_size = min(batch_size, 25)
    batches = [
        browser_nodeids[index : index + browser_batch_size]
        for index in range(0, len(browser_nodeids), browser_batch_size)
    ]
    current: list[str] = []
    for module_nodeids in modules.values():
        if current and len(current) + len(module_nodeids) > batch_size:
            batches.append(current)
            current = []
        current.extend(module_nodeids)
    if current:
        batches.append(current)
    return batches


def _write_shard_manifests(
    nodeids_file: Path,
    batch_size: int,
    work_dir: Path,
    playwright_nodeids: set[str] | None = None,
) -> Path:
    nodeids = nodeids_file.read_text(encoding="utf-8").splitlines()
    batches = _batch_nodeids(nodeids, batch_size, playwright_nodeids)
    manifests_dir = work_dir / "shards"
    manifests_dir.mkdir()
    manifest_paths: list[str] = []
    for index, batch in enumerate(batches):
        manifest = manifests_dir / f"shard-{index}.json"
        manifest.write_text(json.dumps(batch), encoding="utf-8")
        manifest_paths.append(str(manifest))
    shards_file = work_dir / "shards.txt"
    shards_file.write_text("\n".join(manifest_paths) + "\n", encoding="utf-8")
    print(f"Packed {len(nodeids)} nodeids into {len(batches)} shards.")
    return shards_file


def _merge_summary(reports: list[JsonObject], exit_code: int) -> JsonObject:
    merged_summary: dict[str, int] = {}
    tests: list[object] = []
    warnings: list[object] = []
    first_report: JsonObject | None = None

    for report in reports:
        if first_report is None:
            first_report = report
        summary = report.get("summary", {})
        if isinstance(summary, dict):
            for key, value in summary.items():
                if isinstance(value, int):
                    merged_summary[key] = merged_summary.get(key, 0) + value
        report_tests = report.get("tests", [])
        if isinstance(report_tests, list):
            tests.extend(report_tests)
        report_warnings = report.get("warnings", [])
        if isinstance(report_warnings, list):
            warnings.extend(report_warnings)

    duration = 0.0
    for report in reports:
        report_duration = report.get("duration", 0)
        if isinstance(report_duration, int | float):
            duration += report_duration

    return {
        "created": time.time(),
        "duration": duration,
        "exitcode": exit_code,
        "root": first_report.get("root") if first_report else str(_repo_root()),
        "environment": first_report.get("environment", {}) if first_report else {},
        "summary": merged_summary,
        "tests": tests,
        "warnings": warnings,
    }


def _write_merged_json_report(
    report_file: Path, shard_reports_dir: Path, exit_code: int
) -> None:
    reports: list[JsonObject] = []
    for shard_report_file in sorted(shard_reports_dir.glob("shard-*.json")):
        report = json.loads(shard_report_file.read_text(encoding="utf-8"))
        if isinstance(report, dict):
            reports.append(cast("JsonObject", report))

    if not report_file.is_absolute():
        report_file = _repo_root() / report_file
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(
        json.dumps(_merge_summary(reports, exit_code), indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    pytest_bin = _pytest_bin()
    parallel_bin = _parallel_bin()
    json_report_file = _json_report_file(sys.argv[1:])
    pytest_args = _sanitize_pytest_args(sys.argv[1:])
    collection_pytest_args = _with_default_collection_selector(pytest_args)
    shard_pytest_args = _strip_collection_selectors(pytest_args)

    work_dir = Path(os.environ.get("PYTEST_ADAPTIVE_DIR", ".pytest-adaptive"))
    if not work_dir.is_absolute():
        work_dir = _repo_root() / work_dir
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)

    nodeids_file = work_dir / "nodeids.txt"
    collect_exit = _collect_nodeids(pytest_bin, collection_pytest_args, nodeids_file)
    if collect_exit != 0:
        return collect_exit

    if nodeids_file.stat().st_size == 0:
        print("No pytest nodeids collected.")
        return PYTEST_EXIT_NO_TESTS_COLLECTED

    jobs = os.environ.get("PYTEST_ADAPTIVE_JOBS", "12")
    nodeid_count = len(nodeids_file.read_text(encoding="utf-8").splitlines())
    # Leave several scheduling waves for load balancing, without restarting pytest
    # hundreds of times for a large suite. GNU Parallel also accepts nonnumeric jobs.
    default_batch_size = max(
        25,
        math.ceil(nodeid_count / (max(1, int(jobs)) * 4)) if jobs.isdecimal() else 25,
    )
    batch_size = int(os.environ.get("PYTEST_ADAPTIVE_BATCH_SIZE", default_batch_size))
    collection = json.loads(
        nodeids_file.with_suffix(".json").read_text(encoding="utf-8")
    )
    shards_file = _write_shard_manifests(
        nodeids_file, batch_size, work_dir, set(collection["playwright_nodeids"])
    )
    load_limit = os.environ.get("PYTEST_ADAPTIVE_LOAD", "100%")
    mem_threshold = os.environ.get("PYTEST_ADAPTIVE_MEM_THRESHOLD", "0.80")
    delay = os.environ.get("PYTEST_ADAPTIVE_DELAY", "0.2")
    joblog = work_dir / "joblog.tsv"
    results_dir = work_dir / "results" / "{#}"
    shard_reports_dir = work_dir / "json-reports"

    base_command = [
        pytest_bin,
        "-q",
        "--tb=short",
        "--disable-warnings",
        "-n0",
        *shard_pytest_args,
    ]
    env = os.environ.copy()
    env["PYTEST_ADAPTIVE_BASE_COMMAND"] = json.dumps(base_command)
    if json_report_file is not None:
        shard_reports_dir.mkdir()
        env["PYTEST_ADAPTIVE_SHARD_REPORT_DIR"] = str(shard_reports_dir)

    limit_command = " ".join([
        shlex.quote(sys.executable),
        shlex.quote(str(_repo_root() / "scripts" / "cgroup_memory_gate.py")),
        shlex.quote(mem_threshold),
    ])
    shard_runner = _repo_root() / "scripts" / "run_pytest_shard.py"
    command = [
        parallel_bin,
        "--will-cite",
        "--delimiter",
        "\n",
        "-N",
        "1",
        "--jobs",
        jobs,
        "--load",
        load_limit,
        "--limit",
        limit_command,
        "--delay",
        delay,
        "--joblog",
        str(joblog),
        "--results",
        str(results_dir),
        "--line-buffer",
        shlex.quote(sys.executable),
        shlex.quote(str(shard_runner)),
        "--adaptive-manifest",
        "{}",
        "::::",
        str(shards_file),
    ]

    print(
        "Running adaptive pytest: "
        f"batch_size={batch_size}, jobs={jobs}, load={load_limit}, "
        f"mem_threshold={mem_threshold}, delay={delay}"
    )
    result = subprocess.run(command, check=False, cwd=_repo_root(), env=env)
    if json_report_file is not None:
        _write_merged_json_report(
            json_report_file,
            shard_reports_dir,
            result.returncode,
        )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
