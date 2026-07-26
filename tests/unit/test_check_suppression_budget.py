"""Tests for the shrink-only lint suppression budget check.

The check is only worth having if it survives the obvious ways of getting past
it, so each route to a new suppression gets a test: a `per-file-ignores` entry,
an inline suppression comment, a file-level one, and switching the rule off.
"""

import importlib.util
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


def _bare_suppress() -> str:
    """Build a suppression comment with no codes, which silences everything."""
    return "# " + "noqa"


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


def test_counts_bare_noqa_against_every_budget(repo: Path) -> None:
    """A directive with no codes silences every rule on the line, budget included."""
    _write_repo(
        repo,
        budget=0,
        sources={
            "src/a.py": f"try:  {_bare_suppress()}\n    pass\nexcept Exception:\n    pass\n"
        },
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

    # Nonzero so the rewritten file has to reach the commit; a run that lowered
    # the ceiling and passed would leave the old ceiling in the merged tree.
    assert module.check(repo) == 1
    assert module.read_budgets(repo) == {"PLW0717": 1}


def test_passes_once_lowered_budget_is_committed(repo: Path) -> None:
    _write_repo(repo, budget=5, per_file_ignores={"src/a.py": ["PLW0717"]})
    module = _script()
    module.check(repo)

    assert module.check(repo) == 0


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
    """The committed budget matches reality, so the check is meaningful in CI.

    Counted directly rather than by running `check()`, which would rewrite
    `.lint-budget.toml` as a side effect of a test.
    """
    module = _script()
    root = module.REPO_ROOT
    budgets = module.read_budgets(root)
    assert budgets, "expected at least one budgeted rule"

    assert not module.globally_ignored(root, sorted(budgets))
    usages = module.find_usages(root, sorted(budgets))
    over = {
        rule: (usage.total, budgets[rule])
        for rule, usage in usages.items()
        if usage.total > budgets[rule]
    }
    assert not over, f"suppression counts exceed their budgets: {over}"
