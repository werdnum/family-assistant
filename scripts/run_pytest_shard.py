#!/usr/bin/env python3
"""Run one adaptive pytest shard."""

from __future__ import annotations

import json
import os
import subprocess
import sys

PYTEST_EXIT_NO_TESTS_COLLECTED = 5


def main() -> int:
    """Run pytest for nodeids supplied by GNU Parallel."""
    base_command_text = os.environ["PYTEST_ADAPTIVE_BASE_COMMAND"]
    base_command = json.loads(base_command_text)
    if not isinstance(base_command, list) or not all(
        isinstance(item, str) for item in base_command
    ):
        raise TypeError("PYTEST_ADAPTIVE_BASE_COMMAND must be a JSON list of strings")

    command = [*base_command, *sys.argv[1:]]
    result = subprocess.run(command, check=False)
    if result.returncode == PYTEST_EXIT_NO_TESTS_COLLECTED:
        return 0
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
