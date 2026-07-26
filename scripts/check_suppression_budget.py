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

What gets counted is *suppressed violations*, not suppression directives, and
ruff does the counting: the budget is the number of diagnostics that exist but
go unreported. Asking ruff has two consequences worth knowing. It closes every
way of spelling a suppression at once — a code, a prefix such as `PLW`, a bare
directive, a file-wide `per-file-ignores` entry — because ruff resolves all of
them itself. And it means a file-wide entry stops being a blank cheque: adding
another long `try` to an already-ignored file raises the count and fails, where
counting directives would have let it through for free.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BUDGET_FILENAME = ".lint-budget.toml"

# Replaces the configured per-file-ignores with a mapping that matches nothing,
# so a run sees the violations those entries would have hidden. Everything else
# — excludes, preview, target-version — still comes from the project config.
_NO_PER_FILE_IGNORES = "__lint_budget_no_such_path__/*.py:E501"


def _ruff() -> str:
    """The ruff to ask, preferring the one in this interpreter's environment."""
    candidate = Path(sys.executable).parent / "ruff"
    return str(candidate) if candidate.exists() else "ruff"


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
        "# Each number is how many violations of that ruff rule exist but go\n"
        "# unreported, whether silenced by per-file-ignores or by a suppression\n"
        "# comment. The check fails if a count rises above its budget, and lowers\n"
        "# the budget automatically when a count falls. Do not raise a number here\n"
        "# to make a lint failure go away — fix the code the rule is pointing at.\n"
        "\n[budgets]\n"
    )
    body = "".join(
        f'"{rule}" = {ceiling}\n' for rule, ceiling in sorted(budgets.items())
    )
    (root / BUDGET_FILENAME).write_text(header + body, encoding="utf-8")


def _count_by_code(root: Path, extra_args: list[str]) -> Counter[str]:
    """Run ruff and tally the diagnostics it reports, keyed by rule code."""
    result = subprocess.run(
        [_ruff(), "check", "--output-format", "json", *extra_args, "."],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if not result.stdout.strip():
        # No diagnostics at all, or ruff refused the invocation; the latter is a
        # bug in this script rather than a clean repository, so say which.
        if result.returncode not in {0, 1}:
            raise RuntimeError(
                f"ruff failed ({result.returncode}): {result.stderr.strip()}"
            )
        return Counter()
    return Counter(
        item["code"] for item in json.loads(result.stdout) if item.get("code")
    )


def enabled_rules(root: Path) -> set[str]:
    """The rule codes ruff would actually report, per its own resolved settings.

    Read from ruff rather than from `select`/`ignore`, so `preview = false`,
    `extend-ignore`, and selector precedence are all accounted for.
    """
    result = subprocess.run(
        [_ruff(), "check", "--show-settings", "."],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    codes: set[str] = set()
    in_block = False
    for line in result.stdout.splitlines():
        if line.startswith("linter.rules.enabled"):
            in_block = True
            continue
        if in_block:
            if line.startswith("]"):
                break
            _, _, code = line.strip().rstrip(",").rpartition("(")
            if code.endswith(")"):
                codes.add(code[:-1])
    return codes


def count_suppressed(root: Path, rules: list[str]) -> dict[str, int]:
    """How many violations of each rule exist but are not reported."""
    reported = _count_by_code(root, [])
    unsuppressed = _count_by_code(
        root,
        [
            "--per-file-ignores",
            _NO_PER_FILE_IGNORES,
            "--ignore-noqa",
            "--select",
            ",".join(rules),
        ],
    )
    return {rule: max(0, unsuppressed[rule] - reported[rule]) for rule in rules}


def check(root: Path) -> int:
    """Report on every budgeted rule; return a process exit status."""
    budgets = read_budgets(root)
    if not budgets:
        print(f"No suppression budgets configured in {BUDGET_FILENAME}.")
        return 0

    rules = sorted(budgets)

    disabled = sorted(set(rules) - enabled_rules(root))
    if disabled:
        print(
            f"Budgeted rules are not enabled in ruff's resolved settings: "
            f"{', '.join(disabled)}.\n"
            "A budget only means something while the rule is still reported. Either\n"
            "re-enable the rule, or delete its budget deliberately.",
            file=sys.stderr,
        )
        return 1

    counts = count_suppressed(root, rules)
    failed = False
    lowered: dict[str, int] = {}

    for rule in rules:
        total = counts[rule]
        budget = budgets[rule]
        if total > budget:
            failed = True
            print(
                f"{rule}: {total} suppressed violations, budget is {budget}.\n"
                f"\n"
                f"  Do not raise the budget in {BUDGET_FILENAME} to get past this.\n"
                f"  The budget exists because silencing {rule} is easier than fixing it,\n"
                f"  and it is meant to shrink. Fix the code the rule points at — for\n"
                f"  PLW0717 that means narrowing the `try` to the statements that can\n"
                f"  actually raise, and moving the rest out of it.\n"
                f"\n"
                f"  Note this counts violations, not `noqa` comments, so a file that\n"
                f"  already has a per-file-ignores entry still costs budget when you\n"
                f"  add another violation to it.\n"
                f"\n"
                f"  If the suppression really is correct, say so on the pull request and\n"
                f"  get a human to agree before touching the budget.\n",
                file=sys.stderr,
            )
        elif total < budget:
            lowered[rule] = total

    if lowered:
        budgets.update(lowered)
        write_budgets(root, budgets)
        for rule, total in sorted(lowered.items()):
            print(
                f"{rule}: budget lowered to {total}. Commit {BUDGET_FILENAME}.",
                file=sys.stderr,
            )
        # Nonzero even though nothing is wrong with the code: the rewritten file
        # has to reach the commit, or the ceiling stays high and the headroom the
        # fix just freed is available to the next suppression.
        failed = True

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
