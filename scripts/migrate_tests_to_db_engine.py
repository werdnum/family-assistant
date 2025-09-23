#!/usr/bin/env python3
"""
Automated migration script to add explicit db_engine parameter to test functions.

This script uses ast-grep scan to automatically update test functions to use the new
parameterized db_engine fixture instead of relying on the autouse test_db_engine.
"""

import argparse
import subprocess
import sys
from pathlib import Path

# Define the ast-grep rules - keep it simple for now
# We'll use a two-pass approach:
# 1. Replace test_db_engine with db_engine everywhere
# 2. Manually add db_engine parameter to tests that need it
MIGRATION_RULES = """
id: replace-test-db-engine
language: python
rule:
  pattern: test_db_engine
fix: db_engine
"""


def _count_matches_from_output(output: str) -> int:
    """Approximate the number of matches reported by ast-grep output."""
    matches = 0
    if output:
        for line in output.split("\n"):
            if "test_" in line and (
                "add-db-engine" in line or "replace-test-db-engine" in line
            ):
                matches += 1
    return matches


def run_ast_grep_scan(paths: list[Path], dry_run: bool = False) -> tuple[bool, int]:
    """Run ast-grep scan with the migration rules on specified paths."""
    cmd = [
        "ast-grep",
        "scan",
        "--inline-rules",
        MIGRATION_RULES,
    ]

    if not dry_run:
        cmd.append("--update-all")

    # Add paths
    cmd.extend(str(p) for p in paths)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
        matches = _count_matches_from_output(result.stdout)
        return True, matches
    except subprocess.CalledProcessError as err:
        stderr = err.stderr or ""
        if stderr:
            print(f"Error: {stderr}")
        matches = _count_matches_from_output(err.stdout or "")
        return False, matches
    except Exception as e:
        print(f"Exception running ast-grep: {e}")
        return False, 0


def find_test_files(root_dir: Path) -> list[Path]:
    """Find all test files in the given directory."""
    return list(root_dir.rglob("test_*.py"))


def count_tests_needing_migration(file_path: Path) -> int:
    """Count how many tests in a file need migration."""
    # This is a simple grep-based count
    try:
        with open(file_path, encoding="utf-8") as f:
            content = f.read()
            return content.count("async def test_")
    except Exception:
        return 0


def main() -> None:
    # Get the repository root
    script_dir = Path(__file__).parent
    repo_root = script_dir.parent
    tests_dir = repo_root / "tests"

    if not tests_dir.exists():
        print(f"Tests directory not found: {tests_dir}")
        sys.exit(1)

    print("=== Test Migration Script ===")
    print(f"Repository root: {repo_root}")
    print(f"Tests directory: {tests_dir}")

    parser = argparse.ArgumentParser(
        description="Migrate tests to use db_engine fixture"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without modifying files",
    )
    parser.add_argument(
        "--path",
        help="Specific path to migrate (default: all tests)",
        default=str(tests_dir),
    )
    args = parser.parse_args()

    # Find all test files
    target_path = Path(args.path)
    if target_path.is_file():
        test_files = [target_path]
    else:
        test_files = find_test_files(target_path)

    # Filter out conftest.py
    test_files = [f for f in test_files if f.name != "conftest.py"]

    print(f"\nFound {len(test_files)} test files")

    # Count total tests
    total_tests = sum(count_tests_needing_migration(f) for f in test_files)
    print(f"Total test functions: ~{total_tests}")

    if args.dry_run:
        print("\nDRY RUN MODE - no files will be modified")
    else:
        # Ask for confirmation
        response = input("\nProceed with migration? (y/N): ")
        if response.lower() != "y":
            print("Migration cancelled.")
            sys.exit(0)

    # Run ast-grep scan on all files at once
    print("\nRunning ast-grep scan...")
    success, matches = run_ast_grep_scan(test_files, dry_run=args.dry_run)

    if success:
        print("\n=== Migration Summary ===")
        if args.dry_run:
            print(f"Would modify approximately {matches} locations")
            print("\nRun without --dry-run to apply changes")
        else:
            print(f"Modified approximately {matches} locations")
            print("\n=== Next Steps ===")
            print("1. Review the changes with: git diff")
            print("2. Run the tests to ensure they still pass")
            print("3. Update any custom fixtures that depend on test_db_engine")
            print(
                "4. Consider marking PostgreSQL-specific tests with @pytest.mark.postgres"
            )
    else:
        print("\nMigration failed. Please check the errors above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
