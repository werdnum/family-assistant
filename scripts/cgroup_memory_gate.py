#!/usr/bin/env python3
"""GNU Parallel --limit helper for cgroup v2 memory pressure."""

from __future__ import annotations

import sys
from pathlib import Path


def memory_usage_ratio(cgroup_path: Path = Path("/sys/fs/cgroup")) -> float | None:
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


def main() -> int:
    """Exit using GNU Parallel --limit semantics."""
    threshold = float(sys.argv[1]) if len(sys.argv) > 1 else 0.80
    ratio = memory_usage_ratio()
    if ratio is None:
        return 0

    return 1 if ratio >= threshold else 0


if __name__ == "__main__":
    raise SystemExit(main())
