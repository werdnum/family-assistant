#!/usr/bin/env python3
"""
Add inline exemption comments for existing ast-grep rule violations.

This script finds all violations of an ast-grep rule and automatically adds
# ast-grep-ignore comments before each violation.

Usage:
    .ast-grep/add-exemptions.py <rule-id> [files...] [--dry-run] [--reason "text"]

Examples:
    # Add exemptions to all violations in the codebase
    .ast-grep/add-exemptions.py no-dict-any

    # Add exemptions to specific files only
    .ast-grep/add-exemptions.py no-dict-any src/file1.py src/file2.py

    # Add exemptions to only changed files
    .ast-grep/add-exemptions.py no-dict-any $(git diff --name-only)

    # Preview changes without modifying files
    .ast-grep/add-exemptions.py no-dict-any --dry-run

    # Add exemptions with custom reason
    .ast-grep/add-exemptions.py no-dict-any --reason "Legacy code needs refactoring"
"""

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


def find_violations(rule_id: str, files: list[str] | None = None) -> list[dict]:
    """Run check-conformance.py to find violations (respects existing exemptions)."""
    # Use check-conformance.py which filters out already-exempted violations
    cmd = [".ast-grep/check-conformance.py", "--json"]

    # Add specific files if provided
    if files:
        cmd.extend(files)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)

        # Check for errors - fail loudly instead of silently
        if result.returncode != 0:
            error_msg = (
                result.stderr.strip()
                if result.stderr
                else f"Exit code {result.returncode}"
            )
            print(f"Error running check-conformance.py: {error_msg}", file=sys.stderr)
            sys.exit(1)

        if not result.stdout:
            return []

        # Parse JSON output
        all_violations = json.loads(result.stdout)

        # Filter to only the specified rule
        violations = [v for v in all_violations if v.get("ruleId") == rule_id]
        return violations

    except json.JSONDecodeError as e:
        print(f"Error parsing ast-grep output: {e}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print("Error: ast-grep not found in PATH", file=sys.stderr)
        sys.exit(1)


def get_indentation(line: str) -> str:
    """Extract the indentation (leading whitespace) from a line."""
    match = re.match(r"^(\s*)", line)
    return match.group(1) if match else ""


def has_exemption_above(lines: list[str], line_idx: int, rule_id: str) -> bool:
    """Check if there's already an exemption comment on the line before."""
    if line_idx == 0:
        return False

    prev_line = lines[line_idx - 1].strip()
    return f"ast-grep-ignore: {rule_id}" in prev_line


def add_exemptions_to_file(
    file_path: str,
    violations: list[dict],
    rule_id: str,
    reason: str,
    dry_run: bool,
) -> int:
    """Add exemption comments to a file for the specified violations."""
    try:
        with open(file_path, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        print(f"Error reading {file_path}: {e}", file=sys.stderr)
        return 0

    # Sort violations by line number (descending) so we can insert from bottom to top
    # This way line numbers don't shift as we insert
    violations_sorted = sorted(
        violations,
        key=lambda v: v.get("range", {}).get("start", {}).get("line", 0),
        reverse=True,
    )

    added_count = 0
    for violation in violations_sorted:
        # ast-grep reports 0-based line numbers in JSON
        line_num = violation.get("range", {}).get("start", {}).get("line", 0)

        # Check if exemption already exists
        if has_exemption_above(lines, line_num, rule_id):
            continue

        # Get indentation from the violation line
        indent = get_indentation(lines[line_num]) if line_num < len(lines) else ""

        # Create exemption comment with same indentation
        exemption_comment = f"{indent}# ast-grep-ignore: {rule_id} - {reason}\n"

        # Insert the comment
        lines.insert(line_num, exemption_comment)
        added_count += 1

    if added_count > 0:
        if dry_run:
            print(f"Would add {added_count} exemption(s) to {file_path}")
        else:
            # Write the modified file
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.writelines(lines)
                print(f"Added {added_count} exemption(s) to {file_path}")
            except Exception as e:
                print(f"Error writing {file_path}: {e}", file=sys.stderr)
                return 0

    return added_count


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add inline ast-grep-ignore comments for rule violations"
    )
    parser.add_argument("rule_id", help="Rule ID (e.g., no-dict-any)")
    parser.add_argument(
        "files",
        nargs="*",
        default=argparse.SUPPRESS,
        help="Optional list of files to process (default: all files)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without modifying files",
    )
    parser.add_argument(
        "--reason",
        default="TODO: Add reason for exemption",
        help="Reason text for exemption comment",
    )

    args = parser.parse_args()

    # Get the files to scan
    files_to_scan = getattr(args, "files", None)

    # If an empty list of files was provided, do nothing
    if files_to_scan is not None and not files_to_scan:
        print("No files to process.")
        return 0

    # Verify rule exists
    rule_file = Path(".ast-grep/rules") / f"{args.rule_id}.yml"
    if not rule_file.exists():
        # Also check in hints subdirectory
        rule_file = Path(".ast-grep/rules/hints") / f"{args.rule_id}.yml"
        if not rule_file.exists():
            print(f"Error: Rule '{args.rule_id}' not found", file=sys.stderr)
            return 1

    # Display what we're scanning
    if files_to_scan:
        print(
            f"Finding violations of rule: {args.rule_id} in {len(files_to_scan)} file(s)"
        )
    else:
        print(f"Finding violations of rule: {args.rule_id}")

    violations = find_violations(args.rule_id, files_to_scan)

    if not violations:
        print("No violations found!")
        return 0

    # Group violations by file
    violations_by_file = defaultdict(list)
    for v in violations:
        file_path = v.get("file")
        if file_path:
            violations_by_file[file_path].append(v)

    print(f"Found {len(violations)} violation(s) in {len(violations_by_file)} file(s)")
    if args.dry_run:
        print("(dry-run mode - no files will be modified)")
    print()

    # Add exemptions to each file
    total_added = 0
    for file_path, file_violations in violations_by_file.items():
        added = add_exemptions_to_file(
            file_path,
            file_violations,
            args.rule_id,
            args.reason,
            args.dry_run,
        )
        total_added += added

    print()
    if args.dry_run:
        print(f"Would add {total_added} exemption(s) total")
    else:
        print(f"Added {total_added} exemption(s) total")

    return 0


if __name__ == "__main__":
    sys.exit(main())
