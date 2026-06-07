"""Tests for the adaptive pytest runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType


def _load_runner_module() -> ModuleType:
    script_path = Path(__file__).parents[2] / "scripts" / "run_pytest_adaptive.py"
    spec = importlib.util.spec_from_file_location("run_pytest_adaptive", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {script_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sanitize_pytest_args_strips_quiet_from_compact_fail_fast() -> None:
    runner = _load_runner_module()

    assert runner._sanitize_pytest_args(["--db", "postgres", "-xq"]) == [
        "--db",
        "postgres",
        "-x",
    ]
