"""Tests for the adaptive pytest runner."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

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


def test_batches_keep_module_fixtures_together_and_preserve_all_nodeids() -> None:
    runner = _load_runner_module()
    nodeids = [
        "tests/test_a.py::test_one[sqlite]",
        "tests/test_a.py::test_one[postgres]",
        "tests/test_b.py::test_two",
        "tests/test_b.py::test_three",
        "tests/test_b.py::test_four",
        "tests/test_c.py::test_five",
    ]

    batches = runner._batch_nodeids(nodeids, 2)

    assert batches == [nodeids[:2], nodeids[2:5], nodeids[5:]]
    assert [nodeid for batch in batches for nodeid in batch] == nodeids


def test_batches_combine_small_modules() -> None:
    runner = _load_runner_module()
    nodeids = [f"tests/test_{index}.py::test_case" for index in range(5)]

    assert runner._batch_nodeids(nodeids, 3) == [nodeids[:3], nodeids[3:]]


def test_batches_empty_collection() -> None:
    runner = _load_runner_module()

    assert runner._batch_nodeids([], 25) == []


@pytest.mark.parametrize("fails", [False, True])
def test_shard_manifest_executes_exact_selection_and_propagates_failure(
    tmp_path: Path, fails: bool
) -> None:
    runner = _load_runner_module()
    test_module = tmp_path / "test_example.py"
    test_module.write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('value', ['spaces and :: delimiters'])\n"
        f"def test_selected(value):\n    assert {not fails}\n"
        "def test_unselected():\n    assert False\n",
        encoding="utf-8",
    )
    nodeid = f"{test_module}::test_selected[spaces and :: delimiters]"
    nodeids_file = tmp_path / "nodeids.txt"
    nodeids_file.write_text(nodeid + "\n", encoding="utf-8")
    manifests = runner._write_shard_manifests(nodeids_file, 25, tmp_path)
    manifest = manifests.read_text(encoding="utf-8").strip()
    environment = os.environ.copy()
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PYTEST_ADAPTIVE_BASE_COMMAND"] = json.dumps([
        sys.executable,
        "-m",
        "pytest",
        "-q",
    ])
    environment.pop("PYTEST_ADAPTIVE_SHARD_REPORT_DIR", None)
    environment.pop("PYTEST_ADDOPTS", None)
    script = Path(__file__).parents[2] / "scripts" / "run_pytest_shard.py"

    result = subprocess.run(
        [sys.executable, str(script), "--adaptive-manifest", manifest],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == int(fails), result.stdout + result.stderr
    assert ("1 failed" if fails else "1 passed") in result.stdout
