"""Tests for the shrink-only lint suppression budget check.

The check is only worth having if it survives the ways of getting past it, so
each one gets a case: a `per-file-ignores` entry, a prefix selector, a
suppression comment naming the code, a bare suppression comment, and disabling
the rule outright — whether by `ignore`, `extend-ignore`, or turning `preview`
off under a preview rule.

Fixtures are real repositories with real violations, because the check asks ruff
for the counts rather than reading the config itself.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_suppression_budget.py"


def _script() -> ModuleType:
    """Import the check as a module; it takes the repository root as an argument."""
    spec = importlib.util.spec_from_file_location("check_suppression_budget", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _violating_module(count: int = 1, *, trailing_comment: str = "") -> str:
    """A module with `count` try clauses long enough to trip PLW0717."""
    functions = []
    for n in range(count):
        body = "\n".join(f"        x{i} = {i}" for i in range(8))
        functions.append(
            f"def f{n}() -> None:\n"
            f"    try:{trailing_comment}\n"
            f"{body}\n"
            f"    except Exception:\n"
            f"        pass\n"
        )
    return "\n\n".join(functions)


def _write_repo(
    root: Path,
    *,
    budget: int,
    sources: dict[str, str],
    per_file_ignores: dict[str, list[str]] | None = None,
    ignore: list[str] | None = None,
    extend_ignore: list[str] | None = None,
    preview: bool = True,
) -> None:
    def toml_list(values: list[str]) -> str:
        return "[" + ", ".join(f'"{v}"' for v in values) + "]"

    lines = [
        "[tool.ruff]",
        f"preview = {str(preview).lower()}",
        "",
        "[tool.ruff.lint]",
        'select = ["PLW"]',
        f"ignore = {toml_list(ignore or [])}",
    ]
    if extend_ignore is not None:
        lines.append(f"extend-ignore = {toml_list(extend_ignore)}")
    lines.append("")
    lines.append("[tool.ruff.lint.per-file-ignores]")
    for pattern, codes in (per_file_ignores or {}).items():
        lines.append(f'"{pattern}" = {toml_list(codes)}')

    (root / "pyproject.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (root / ".lint-budget.toml").write_text(
        f'[budgets]\n"PLW0717" = {budget}\n', encoding="utf-8"
    )
    for name, body in sources.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return tmp_path


def test_unsuppressed_violations_do_not_count(repo: Path) -> None:
    """A violation ruff reports is a lint failure, not a suppression."""
    _write_repo(repo, budget=0, sources={"a.py": _violating_module()})
    assert _script().check(repo) == 0


def test_counts_violations_hidden_by_per_file_ignores(repo: Path) -> None:
    _write_repo(
        repo,
        budget=0,
        sources={"a.py": _violating_module()},
        per_file_ignores={"a.py": ["PLW0717"]},
    )
    assert _script().check(repo) == 1


def test_counts_violations_hidden_by_prefix_selector(repo: Path) -> None:
    """`["PLW"]` suppresses PLW0717 just as `["PLW0717"]` does."""
    _write_repo(
        repo,
        budget=0,
        sources={"a.py": _violating_module()},
        per_file_ignores={"a.py": ["PLW"]},
    )
    assert _script().check(repo) == 1


def test_counts_violations_hidden_by_suppression_comment(repo: Path) -> None:
    _write_repo(
        repo,
        budget=0,
        sources={"a.py": _violating_module(trailing_comment="  # " + "noqa: PLW0717")},
    )
    assert _script().check(repo) == 1


def test_counts_violations_hidden_by_bare_suppression_comment(repo: Path) -> None:
    """A directive with no codes silences everything on the line, budget included."""
    _write_repo(
        repo,
        budget=0,
        sources={"a.py": _violating_module(trailing_comment="  # " + "noqa")},
    )
    assert _script().check(repo) == 1


def test_already_ignored_file_is_not_a_blank_cheque(repo: Path) -> None:
    """Adding a violation to an ignored file costs budget, unlike counting directives."""
    _write_repo(
        repo,
        budget=1,
        sources={"a.py": _violating_module(count=1)},
        per_file_ignores={"a.py": ["PLW0717"]},
    )
    assert _script().check(repo) == 0

    _write_repo(
        repo,
        budget=1,
        sources={"a.py": _violating_module(count=2)},
        per_file_ignores={"a.py": ["PLW0717"]},
    )
    assert _script().check(repo) == 1


def test_fails_when_rule_is_globally_ignored(repo: Path) -> None:
    _write_repo(
        repo,
        budget=1,
        sources={"a.py": _violating_module()},
        ignore=["PLW0717"],
        per_file_ignores={"a.py": ["PLW0717"]},
    )
    assert _script().check(repo) == 1


def test_fails_when_rule_is_extend_ignored(repo: Path) -> None:
    _write_repo(
        repo,
        budget=1,
        sources={"a.py": _violating_module()},
        extend_ignore=["PLW0717"],
        per_file_ignores={"a.py": ["PLW0717"]},
    )
    assert _script().check(repo) == 1


def test_fails_when_preview_is_disabled(repo: Path) -> None:
    """PLW0717 is a preview rule, so preview = false silences it wholesale."""
    _write_repo(
        repo,
        budget=1,
        sources={"a.py": _violating_module()},
        per_file_ignores={"a.py": ["PLW0717"]},
        preview=False,
    )
    assert _script().check(repo) == 1


def test_lowers_budget_when_count_drops(repo: Path) -> None:
    _write_repo(
        repo,
        budget=5,
        sources={"a.py": _violating_module()},
        per_file_ignores={"a.py": ["PLW0717"]},
    )
    module = _script()

    # Nonzero so the rewritten file has to reach the commit; a run that lowered
    # the ceiling and passed would leave the old ceiling in the merged tree.
    assert module.check(repo) == 1
    assert module.read_budgets(repo) == {"PLW0717": 1}

    assert module.check(repo) == 0


def test_lowered_budget_is_then_enforced(repo: Path) -> None:
    _write_repo(
        repo,
        budget=5,
        sources={"a.py": _violating_module(count=1)},
        per_file_ignores={"a.py": ["PLW0717"]},
    )
    module = _script()
    module.check(repo)

    _write_repo(
        repo,
        budget=module.read_budgets(repo)["PLW0717"],
        sources={"a.py": _violating_module(count=2)},
        per_file_ignores={"a.py": ["PLW0717"]},
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

    assert not set(budgets) - module.enabled_rules(root)
    counts = module.count_suppressed(root, sorted(budgets))
    over = {
        rule: (total, budgets[rule])
        for rule, total in counts.items()
        if total > budgets[rule]
    }
    assert not over, f"suppression counts exceed their budgets: {over}"
