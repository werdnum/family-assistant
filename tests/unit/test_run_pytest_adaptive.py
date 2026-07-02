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


def test_adaptive_collection_defaults_to_tests_without_explicit_selector() -> None:
    runner = _load_runner_module()

    assert runner._with_default_collection_selector([
        "--db",
        "sqlite",
        "-m",
        "not playwright",
        "--timeout=300",
    ]) == ["--db", "sqlite", "-m", "not playwright", "--timeout=300", "tests"]


def test_adaptive_collection_preserves_explicit_path_selector() -> None:
    runner = _load_runner_module()

    assert runner._with_default_collection_selector([
        "--db",
        "sqlite",
        "tests/unit/test_run_pytest_adaptive.py",
    ]) == ["--db", "sqlite", "tests/unit/test_run_pytest_adaptive.py"]
