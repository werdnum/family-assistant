#!/usr/bin/env python3
"""Run one adaptive pytest shard."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PYTEST_EXIT_NO_TESTS_COLLECTED = 5


def main() -> int:
    """Run pytest for nodeids supplied by GNU Parallel."""
    args = sys.argv[1:]
    shard_id = None
    if len(args) >= 2 and args[0] == "--adaptive-shard-id":
        shard_id = args[1]
        args = args[2:]

    base_command_text = os.environ["PYTEST_ADAPTIVE_BASE_COMMAND"]
    base_command = json.loads(base_command_text)
    if not isinstance(base_command, list) or not all(
        isinstance(item, str) for item in base_command
    ):
        raise TypeError("PYTEST_ADAPTIVE_BASE_COMMAND must be a JSON list of strings")

    shard_report_dir = os.environ.get("PYTEST_ADAPTIVE_SHARD_REPORT_DIR")
    if shard_report_dir is not None:
        report_file = Path(shard_report_dir) / f"shard-{shard_id or os.getpid()}.json"
        report_file.parent.mkdir(parents=True, exist_ok=True)
        base_command = [
            *base_command,
            "--json-report",
            f"--json-report-file={report_file}",
        ]

    command = [*base_command, *args]
    result = subprocess.run(command, check=False)
    if result.returncode == PYTEST_EXIT_NO_TESTS_COLLECTED:
        return 0
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
