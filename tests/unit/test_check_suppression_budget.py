"""Tests for the shrink-only lint suppression budget check.

The check is only worth having if it survives the obvious ways of getting past
it, so each route to a new suppression gets a test: a `per-file-ignores` entry,
an inline suppression comment, a file-level one, and switching the rule off.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_suppression_budget.py"


def _suppress(codes: str, *, file_level: bool = False) -> str:
    """Build a suppression comment for a fixture.

    Assembled rather than written literally: the check counts by text, so a
    literal suppression comment in this file would count against the real
    repository budget.
    """
    prefix = "# ruff: " if file_level else "# "
    return prefix + "noqa" + f": {codes}"


def _script() -> ModuleType:
    """Import the check as a module; it takes the repository root as an argument."""
    spec = importlib.util.spec_from_file_location("check_suppression_budget", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so the module's dataclasses can resolve their own
    # postponed annotations.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_repo(
    root: Path,
    *,
    budget: int,
    per_file_ignores: dict[str, list[str]] | None = None,
    sources: dict[str, str] | None = None,
    ignore: list[str] | None = None,
) -> None:
    ignore_entries = "".join(f'    "{code}",\n' for code in ignore or [])
    per_file = "".join(
        f'"{pattern}" = {codes!r}\n'.replace("'", '"')
        for pattern, codes in (per_file_ignores or {}).items()
    )
    (root / "pyproject.toml").write_text(
        "[tool.ruff.lint]\n"
        'select = ["PLW"]\n'
        f"ignore = [\n{ignore_entries}]\n"
        f"\n[tool.ruff.lint.per-file-ignores]\n{per_file}",
        encoding="utf-8",
    )
    (root / ".lint-budget.toml").write_text(
        f'[budgets]\n"PLW0717" = {budget}\n', encoding="utf-8"
    )
    for name, body in (sources or {}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return tmp_path


def test_passes_when_count_matches_budget(repo: Path) -> None:
    _write_repo(repo, budget=1, per_file_ignores={"src/a.py": ["PLW0717"]})
    assert _script().check(repo) == 0


def test_fails_on_new_per_file_ignore(repo: Path) -> None:
    _write_repo(
        repo,
        budget=1,
        per_file_ignores={"src/a.py": ["PLW0717"], "src/b.py": ["PLW0717"]},
    )
    assert _script().check(repo) == 1


def test_fails_on_new_inline_noqa(repo: Path) -> None:
    _write_repo(
        repo,
        budget=0,
        sources={"src/a.py": f"x = 1  {_suppress('PLW0717')}\n"},
    )
    assert _script().check(repo) == 1


def test_fails_on_file_level_noqa(repo: Path) -> None:
    _write_repo(
        repo,
        budget=0,
        sources={"src/a.py": _suppress("PLW0717", file_level=True) + "\n"},
    )
    assert _script().check(repo) == 1


def test_counts_noqa_listing_several_codes(repo: Path) -> None:
    _write_repo(
        repo,
        budget=0,
        sources={"src/a.py": f"x = 1  {_suppress('B008, PLW0717')}\n"},
    )
    assert _script().check(repo) == 1


def test_ignores_noqa_for_other_rules(repo: Path) -> None:
    _write_repo(
        repo,
        budget=0,
        sources={"src/a.py": f"x = 1  {_suppress('PLW0718')}\n"},
    )
    assert _script().check(repo) == 0


def test_fails_when_rule_is_globally_ignored(repo: Path) -> None:
    _write_repo(
        repo, budget=1, ignore=["PLW0717"], per_file_ignores={"src/a.py": ["PLW0717"]}
    )
    assert _script().check(repo) == 1


def test_lowers_budget_when_count_drops(repo: Path) -> None:
    _write_repo(repo, budget=5, per_file_ignores={"src/a.py": ["PLW0717"]})
    module = _script()

    assert module.check(repo) == 0
    assert module.read_budgets(repo) == {"PLW0717": 1}


def test_lowered_budget_is_then_enforced(repo: Path) -> None:
    _write_repo(repo, budget=5, per_file_ignores={"src/a.py": ["PLW0717"]})
    module = _script()
    module.check(repo)

    _write_repo(
        repo,
        budget=module.read_budgets(repo)["PLW0717"],
        per_file_ignores={"src/a.py": ["PLW0717"], "src/b.py": ["PLW0717"]},
    )
    assert _script().check(repo) == 1


def test_repository_is_within_its_own_budget() -> None:
    """The committed budget matches reality, so the check is meaningful in CI."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
