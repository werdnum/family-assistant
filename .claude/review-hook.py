#!/usr/bin/env python3
"""
Pre-commit hook for code review using the review-changes.sh script.
Handles formatting/linting, running the review, and processing the results.
"""

import subprocess
import sys


def run_format_and_lint() -> bool:
    """Run format-and-lint.sh on staged files."""
    print("🔍 Running improved pre-commit review workflow...\n")

    # Get list of staged Python files
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        print("❌ Failed to get staged files")
        return False

    staged_files = [f for f in result.stdout.strip().split("\n") if f.endswith(".py")]

    if not staged_files:
        # No Python files to check
        return True

    print("Running format-and-lint.sh on staged files...")
    result = subprocess.run(["scripts/format-and-lint.sh"] + staged_files, check=False)

    if result.returncode != 0:
        print("❌ Formatting/linting failed")
        print("\nFormatting/linting failed. Please fix the issues before committing.")
        return False

    print("✅ Formatting/linting passed\n")
    return True


def main() -> None:
    """Main hook execution."""
    # Run format-and-lint first
    if not run_format_and_lint():
        sys.exit(1)

    # Success
    sys.exit(0)


if __name__ == "__main__":
    main()
