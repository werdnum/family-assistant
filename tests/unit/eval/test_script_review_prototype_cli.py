"""Argument handling of ``scripts/script_review_prototype_eval.py``.

The script is loaded by path, as ``scripts/`` is not an importable package.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import family_assistant.eval.tool_call_review as tool_call_review_eval
from family_assistant.eval import private_paths

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT_PATH = (
    Path(tool_call_review_eval.__file__).parents[3].parent
    / "scripts"
    / "script_review_prototype_eval.py"
)


@pytest.fixture(scope="module")
def cli() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "script_review_prototype_eval_cli", _SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("flag", ["--concurrency", "--seeds"])
def test_non_positive_counts_are_rejected_at_parsing(
    cli: ModuleType, flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        cli.main([flag, "0", "--dry-run"])
    assert "must be at least 1" in capsys.readouterr().err


def test_out_outside_the_private_tree_is_refused(
    cli: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    # The record holds the judge's reason for every trial, which quotes the
    # reviewed content, so it follows the main harness's containment rule.
    exit_code = cli.main(["--dry-run", "--out", str(tmp_path / "run.json")])
    assert exit_code == 1
    assert ".review-eval-local" in capsys.readouterr().err


def test_out_inside_the_private_tree_resolves(
    cli: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(private_paths, "PROJECT_ROOT", tmp_path)
    out_path = tmp_path / ".review-eval-local" / "runs" / "run.json"
    args = cli._parse_args(["--out", str(out_path)])
    assert cli._resolve_out_path(args) == out_path


def test_out_override_allows_an_external_private_location(
    cli: ModuleType, tmp_path: Path
) -> None:
    out_path = tmp_path / "mounted" / "run.json"
    args = cli._parse_args(["--out", str(out_path), "--allow-external-out"])
    assert cli._resolve_out_path(args) == out_path
