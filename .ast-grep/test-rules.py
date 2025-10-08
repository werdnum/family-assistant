#!/usr/bin/env python3
"""
Test runner for ast-grep rules.

Validates that rules match positive examples and don't match negative examples.

Usage:
    .ast-grep/test-rules.py                    # Test all rules
    .ast-grep/test-rules.py --rule async-magic-mock  # Test specific rule
    .ast-grep/test-rules.py --verbose          # Show detailed output
"""

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TestResult:
    """Result of running a rule test."""

    rule_id: str
    passed: bool
    message: str
    details: str = ""


def find_rule_file(rule_id: str, rule_type: str = "both") -> Path | None:
    """Find the rule file for a given rule ID."""
    if rule_type in {"both", "hints"}:
        hints_path = Path(f".ast-grep/rules/hints/{rule_id}.yml")
        if hints_path.exists():
            return hints_path

    if rule_type in {"both", "conformance"}:
        conformance_path = Path(f".ast-grep/rules/{rule_id}.yml")
        if conformance_path.exists():
            return conformance_path

    return None


def run_rule_on_file(rule_file: Path, test_file: Path) -> list[dict]:
    """Run ast-grep rule on a file and return matches."""
    cmd = ["ast-grep", "scan", "--json", "--rule", str(rule_file), str(test_file)]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)

        if not result.stdout:
            return []

        data = json.loads(result.stdout)
        return data

    except subprocess.CalledProcessError as e:
        print(f"Error running ast-grep: {e}", file=sys.stderr)
        return []
    except json.JSONDecodeError as e:
        print(f"Error parsing ast-grep output: {e}", file=sys.stderr)
        print(f"stdout: {result.stdout}", file=sys.stderr)
        print(f"stderr: {result.stderr}", file=sys.stderr)
        return []


def test_rule(rule_id: str, verbose: bool = False) -> TestResult:
    """Test a single rule against its positive and negative examples."""
    # Find rule file
    rule_file = find_rule_file(rule_id)
    if not rule_file:
        return TestResult(
            rule_id=rule_id,
            passed=False,
            message=f"Rule file not found for {rule_id}",
        )

    # Determine rule type and find test files
    if "hints" in str(rule_file):
        test_dir = Path(".ast-grep/tests/hints")
    else:
        test_dir = Path(".ast-grep/tests/conformance")

    positive_file = test_dir / f"{rule_id}-positive.py"
    negative_file = test_dir / f"{rule_id}-negative.py"

    if not positive_file.exists() and not negative_file.exists():
        return TestResult(
            rule_id=rule_id,
            passed=False,
            message=f"No test files found for {rule_id}",
            details=f"Expected: {positive_file} or {negative_file}",
        )

    failures = []
    details_lines = []

    # Test positive examples (should match)
    if positive_file.exists():
        matches = run_rule_on_file(rule_file, positive_file)
        if not matches:
            failures.append("Positive examples did NOT match (but should)")
            details_lines.append(f"  ❌ {positive_file}: Expected matches, got none")
        elif verbose:
            details_lines.append(
                f"  ✅ {positive_file}: {len(matches)} match(es) found"
            )

    # Test negative examples (should NOT match)
    if negative_file.exists():
        matches = run_rule_on_file(rule_file, negative_file)
        if matches:
            failures.append("Negative examples DID match (but should not)")
            details_lines.append(
                f"  ❌ {negative_file}: Expected no matches, got {len(matches)}"
            )
            if verbose:
                for match in matches:
                    line = match.get("range", {}).get("start", {}).get("line", "?")
                    details_lines.append(f"     Line {line}: {match.get('text', '')}")
        elif verbose:
            details_lines.append(f"  ✅ {negative_file}: No matches (correct)")

    if failures:
        return TestResult(
            rule_id=rule_id,
            passed=False,
            message="; ".join(failures),
            details="\n".join(details_lines),
        )

    return TestResult(
        rule_id=rule_id,
        passed=True,
        message="All tests passed",
        details="\n".join(details_lines) if verbose else "",
    )


def discover_rules() -> list[str]:
    """Discover all rules that have test files."""
    rules = set()

    # Find all test files
    test_dirs = [
        Path(".ast-grep/tests/hints"),
        Path(".ast-grep/tests/conformance"),
    ]

    for test_dir in test_dirs:
        if not test_dir.exists():
            continue

        for test_file in test_dir.glob("*-positive.py"):
            rule_id = test_file.stem.replace("-positive", "")
            rules.add(rule_id)

        for test_file in test_dir.glob("*-negative.py"):
            rule_id = test_file.stem.replace("-negative", "")
            rules.add(rule_id)

    return sorted(rules)


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Test ast-grep rules")
    parser.add_argument(
        "--rule", help="Test specific rule (default: test all rules with test files)"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Show detailed output"
    )

    args = parser.parse_args()

    # Discover rules to test
    rules_to_test = [args.rule] if args.rule else discover_rules()

    if not rules_to_test:
        print("No rules found to test.")
        print(
            "Create test files in .ast-grep/tests/hints/ or .ast-grep/tests/conformance/"
        )
        return 1

    print(f"Testing {len(rules_to_test)} rule(s)...\n")

    # Run tests
    results = []
    for rule_id in rules_to_test:
        result = test_rule(rule_id, verbose=args.verbose)
        results.append(result)

    # Print results
    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed

    for result in results:
        if result.passed:
            print(f"✅ {result.rule_id}: {result.message}")
        else:
            print(f"❌ {result.rule_id}: {result.message}")

        if result.details:
            print(result.details)

    # Summary
    print()
    print(f"Results: {passed} passed, {failed} failed out of {len(results)} total")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
