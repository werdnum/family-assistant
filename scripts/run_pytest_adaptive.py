#!/usr/bin/env python3
"""Run pytest shards through GNU Parallel with live load and cgroup memory gates."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PYTEST_EXIT_NO_TESTS_COLLECTED = 5


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

        if arg in {
            "--disable-warnings",
            "--json-report",
            "--json-report-file",
            "--quiet",
            "--tb",
            "-n",
            "-q",
            "--numprocesses",
        }:
            if arg in {"--json-report-file", "--tb", "-n", "--numprocesses"}:
                skip_next = True
            continue
        if arg.startswith("--json-report-file="):
            continue
        if arg.startswith("--tb="):
            continue
        if arg.startswith("--numprocesses="):
            continue
        if arg.startswith("-n") and arg != "-n0":
            continue
        sanitized.append(arg)

    return sanitized


def _strip_collection_selectors(args: list[str]) -> list[str]:
    """Keep pytest options for shard runs, but drop paths/nodeids already scheduled."""
    option_takes_value = {
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
    shard_args: list[str] = []
    skip_next = False

    for arg in args:
        if skip_next:
            shard_args.append(arg)
            skip_next = False
            continue

        if arg in option_takes_value:
            shard_args.append(arg)
            skip_next = True
            continue
        if arg.startswith("-"):
            shard_args.append(arg)

    return shard_args


def _collect_nodeids(
    pytest_bin: str, pytest_args: list[str], nodeids_file: Path
) -> int:
    collect_report = nodeids_file.with_suffix(".json")
    command = [
        pytest_bin,
        "--collect-only",
        "--json-report",
        f"--json-report-file={collect_report}",
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

    nodeids = [line for line in result.stdout.splitlines() if "::" in line]
    nodeids_file.write_text(
        "\n".join(nodeids) + ("\n" if nodeids else ""),
        encoding="utf-8",
    )
    print(f"Collected {len(nodeids)} pytest nodeids.")
    return 0


def main() -> int:
    pytest_bin = _pytest_bin()
    parallel_bin = _parallel_bin()
    pytest_args = _sanitize_pytest_args(sys.argv[1:])
    shard_pytest_args = _strip_collection_selectors(pytest_args)

    work_dir = Path(os.environ.get("PYTEST_ADAPTIVE_DIR", ".pytest-adaptive"))
    if not work_dir.is_absolute():
        work_dir = _repo_root() / work_dir
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)

    nodeids_file = work_dir / "nodeids.txt"
    collect_exit = _collect_nodeids(pytest_bin, pytest_args, nodeids_file)
    if collect_exit != 0:
        return collect_exit

    if nodeids_file.stat().st_size == 0:
        print("No pytest nodeids collected.")
        return PYTEST_EXIT_NO_TESTS_COLLECTED

    batch_size = os.environ.get("PYTEST_ADAPTIVE_BATCH_SIZE", "25")
    load_limit = os.environ.get("PYTEST_ADAPTIVE_LOAD", "100%")
    mem_threshold = os.environ.get("PYTEST_ADAPTIVE_MEM_THRESHOLD", "0.80")
    delay = os.environ.get("PYTEST_ADAPTIVE_DELAY", "0.2")
    jobs = os.environ.get("PYTEST_ADAPTIVE_JOBS", "12")
    joblog = work_dir / "joblog.tsv"
    results_dir = work_dir / "results" / "{#}"

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

    limit_command = (
        f"{sys.executable} {(_repo_root() / 'scripts' / 'cgroup_memory_gate.py')} "
        f"{mem_threshold}"
    )
    shard_runner = _repo_root() / "scripts" / "run_pytest_shard.py"
    command = [
        parallel_bin,
        "--will-cite",
        "--delimiter",
        "\n",
        "-N",
        batch_size,
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
        sys.executable,
        str(shard_runner),
        "::::",
        str(nodeids_file),
    ]

    print(
        "Running adaptive pytest: "
        f"batch_size={batch_size}, jobs={jobs}, load={load_limit}, "
        f"mem_threshold={mem_threshold}, delay={delay}"
    )
    return subprocess.run(command, check=False, cwd=_repo_root(), env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
