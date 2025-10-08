#!/usr/bin/env python3
"""
Code hints checker using ast-grep.

This script runs ast-grep on hint rules and outputs results in JSON format.
Unlike conformance rules, hints never cause failures and are purely informational.

Usage:
    .ast-grep/check-hints.py [files...]
    .ast-grep/check-hints.py --json [files...]
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def run_ast_grep_hints(files: list[str]) -> list[dict]:
    """Run ast-grep on hint rules and return results."""
    hints_dir = Path(".ast-grep/rules/hints")

    if not hints_dir.exists():
        return []

    # Get all rule files
    rule_files = list(hints_dir.glob("*.yml"))
    if not rule_files:
        return []

    all_results = []

    # Run ast-grep for each rule file
    for rule_file in rule_files:
        cmd = [
            "ast-grep",
            "scan",
            "--json",
            "--rule",
            str(rule_file),
        ]

        if files:
            cmd.extend(files)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)

            if result.stdout:
                # Parse JSON output
                data = json.loads(result.stdout)
                all_results.extend(data)

        except subprocess.CalledProcessError as e:
            print(f"Error running ast-grep with rule {rule_file}: {e}", file=sys.stderr)
            continue
        except json.JSONDecodeError as e:
            print(
                f"Error parsing ast-grep output for rule {rule_file}: {e}",
                file=sys.stderr,
            )
            continue

    # Filter to only test files for test-specific hints
    test_only_hints = {
        "async-magic-mock",
        "test-mocking-guideline",
    }

    filtered_data = []
    for item in all_results:
        file_path = item.get("file", "")
        rule_id = item.get("ruleId", "unknown")

        # Skip test-only hints for non-test files
        if rule_id in test_only_hints and not file_path.startswith("tests/"):
            continue

        filtered_data.append(item)

    return filtered_data


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Check code hints using ast-grep")
    parser.add_argument("files", nargs="*", help="Files to check (default: all)")
    parser.add_argument(
        "--json", action="store_true", help="Output results in JSON format"
    )

    args = parser.parse_args()

    hints = run_ast_grep_hints(args.files)

    if args.json:
        print(json.dumps(hints, indent=2))
    elif hints:
        print("Code Hints:")
        print()
        for hint in hints:
            file_path = hint.get("file", "unknown")
            line_number = hint.get("range", {}).get("start", {}).get("line", "?")
            rule_id = hint.get("ruleId", "unknown")
            message = hint.get("message", "")

            print(f"{file_path}:{line_number}")
            print(f"  💡 [{rule_id}] {message}")
            if "note" in hint:
                # Print note indented
                for line in hint["note"].split("\n"):
                    if line.strip():
                        print(f"     {line}")
            print()
    else:
        print("No hints found.")

    # Always exit 0 - hints never fail
    return 0


if __name__ == "__main__":
    sys.exit(main())
