#!/usr/bin/env python3
"""Enforce a shrink-only budget on lint suppressions for chosen ruff rules.

Some rules describe a habit rather than a bug — `PLW0717`
(too-many-statements-in-try-clause) is the motivating case. Wrapping a long
block in `try` is easy to write, easy to copy from a neighbouring function, and
easy to silence with one more `per-file-ignores` entry, so the suppression count
drifts upwards forever while nobody decides that it should.

This check gives each budgeted rule a recorded ceiling that may fall but never
rise. Silencing a new site fails the build; fixing a site lowers the ceiling so
the room does not come back. The budget lives in `.lint-budget.toml` beside the
count it constrains, which means giving yourself more room is a visible edit to
a file that exists for no other purpose, rather than one more line lost in a
hundred-line ignore list.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BUDGET_FILENAME = ".lint-budget.toml"

# Matches a ruff suppression comment and captures its codes: the inline form,
# the comma-separated form, and the file-level `ruff:` form. Written as a
# concatenation so this source line is not itself a suppression comment.
_NOQA = re.compile(
    r"#\s*(?:ruff:\s*)?"
    + "noqa"
    + r":\s*(?P<codes>[A-Z]+[0-9]+(?:[,\s]+[A-Z]+[0-9]+)*)"
)


@dataclass(frozen=True)
class Usage:
    """Where a rule is currently suppressed, and how many times."""

    per_file_ignores: tuple[str, ...]
    inline: tuple[str, ...]

    @property
    def total(self) -> int:
        return len(self.per_file_ignores) + len(self.inline)


def read_budgets(root: Path) -> dict[str, int]:
    """Read the recorded ceiling for each budgeted rule."""
    budget_file = root / BUDGET_FILENAME
    if not budget_file.exists():
        return {}
    data = tomllib.loads(budget_file.read_text(encoding="utf-8"))
    budgets = data.get("budgets", {})
    return {str(rule): int(ceiling) for rule, ceiling in budgets.items()}


def write_budgets(root: Path, budgets: dict[str, int]) -> None:
    """Rewrite the budget file, preserving its explanatory header."""
    header = (
        "# Suppression budgets, enforced by scripts/check_suppression_budget.py.\n"
        "#\n"
        "# Each number is the count of suppressions for that ruff rule across\n"
        "# per-file-ignores and inline suppression comments. The check fails if a\n"
        "# count rises above its budget, and lowers the budget automatically when\n"
        "# a count falls. Do not raise a number here to make a lint failure go\n"
        "# away — fix the code the rule is pointing at.\n"
        "\n[budgets]\n"
    )
    body = "".join(
        f'"{rule}" = {ceiling}\n' for rule, ceiling in sorted(budgets.items())
    )
    (root / BUDGET_FILENAME).write_text(header + body, encoding="utf-8")


def _lint_config(root: Path) -> dict[str, object]:
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    tool = config["tool"]
    assert isinstance(tool, dict)
    return tool["ruff"]["lint"]


def _python_files(root: Path) -> list[Path]:
    """Every tracked Python file, so the count does not depend on the caller's argv."""
    result = subprocess.run(
        ["git", "ls-files", "-z", "*.py"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [root / name for name in result.stdout.split("\0") if name]


def find_usages(root: Path, rules: list[str]) -> dict[str, Usage]:
    """Count every suppression of each rule, wherever it is expressed."""
    per_file_ignores = _lint_config(root).get("per-file-ignores", {})
    assert isinstance(per_file_ignores, dict)

    inline: dict[str, list[str]] = {rule: [] for rule in rules}
    for path in _python_files(root):
        text = path.read_text(encoding="utf-8")
        if "noqa" not in text:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            match = _NOQA.search(line)
            if match is None:
                continue
            codes = re.split(r"[,\s]+", match.group("codes"))
            for rule in rules:
                if rule in codes:
                    inline[rule].append(f"{path.relative_to(root)}:{lineno}")

    return {
        rule: Usage(
            per_file_ignores=tuple(
                sorted(
                    pattern
                    for pattern, codes in per_file_ignores.items()
                    if rule in codes
                )
            ),
            inline=tuple(inline[rule]),
        )
        for rule in rules
    }


def _longest_match(rule: str, selectors: list[str]) -> int:
    """Length of the longest selector that covers `rule`, or -1 if none does.

    Mirrors ruff's prefix selectors: `PLW` covers `PLW0717`, and a more specific
    selector wins over a broader one on the other side.
    """
    matching = [selector for selector in selectors if rule.startswith(selector)]
    return max((len(selector) for selector in matching), default=-1)


def globally_ignored(root: Path, rules: list[str]) -> list[str]:
    """Budgeted rules that have been switched off wholesale, which defeats the budget."""
    lint_config = _lint_config(root)
    ignored = lint_config.get("ignore", [])
    selected = lint_config.get("select", [])
    assert isinstance(ignored, list)
    assert isinstance(selected, list)
    return [
        rule
        for rule in rules
        if _longest_match(rule, selected) <= _longest_match(rule, ignored)
    ]


def check(root: Path) -> int:
    """Report on every budgeted rule; return a process exit status."""
    budgets = read_budgets(root)
    if not budgets:
        print(f"No suppression budgets configured in {BUDGET_FILENAME}.")
        return 0

    rules = sorted(budgets)

    disabled = globally_ignored(root, rules)
    if disabled:
        print(
            f"Budgeted rules are not enabled in pyproject.toml: {', '.join(disabled)}.\n"
            "A budget only means something while the rule is still reported. Either\n"
            "re-enable the rule, or delete its budget deliberately.",
            file=sys.stderr,
        )
        return 1

    usages = find_usages(root, rules)
    failed = False
    lowered: dict[str, int] = {}

    for rule in rules:
        usage = usages[rule]
        budget = budgets[rule]
        if usage.total > budget:
            failed = True
            print(
                f"{rule}: {usage.total} suppressions, budget is {budget}.\n"
                f"\n"
                f"  Do not raise the budget in {BUDGET_FILENAME} to get past this.\n"
                f"  The budget exists because silencing {rule} is easier than fixing it,\n"
                f"  and it is meant to shrink. Fix the code the rule points at — for\n"
                f"  PLW0717 that means narrowing the `try` to the statements that can\n"
                f"  actually raise, and moving the rest out of it.\n"
                f"\n"
                f"  If the suppression really is correct, say so on the pull request and\n"
                f"  get a human to agree before touching the budget.\n",
                file=sys.stderr,
            )
        elif usage.total < budget:
            lowered[rule] = usage.total

    if lowered:
        budgets.update(lowered)
        write_budgets(root, budgets)
        for rule, total in sorted(lowered.items()):
            print(f"{rule}: budget lowered to {total}. Commit {BUDGET_FILENAME}.")

    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "filenames",
        nargs="*",
        help="Ignored; the whole repository is always counted. Accepted so this "
        "can run as a pre-commit hook.",
    )
    parser.parse_args()
    return check(REPO_ROOT)


if __name__ == "__main__":
    sys.exit(main())
