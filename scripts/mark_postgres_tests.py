#!/usr/bin/env python3
"""
Script to mark tests that use pg_vector_db_engine with @pytest.mark.postgres.

This ensures these tests only run when PostgreSQL is available.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

# Rule to add pytest.mark.postgres to tests using pg_vector_db_engine
ADD_POSTGRES_MARK_RULE = """
id: add-postgres-mark-to-pg-vector-tests
language: python
rule:
  all:
    - pattern: |
        async def test_$NAME($$$PARAMS, pg_vector_db_engine: $TYPE, $$$MORE):
          $$$BODY
    - not:
        has:
          pattern: "@pytest.mark.postgres"
  fix: |
    @pytest.mark.postgres
    async def test_$NAME($$$PARAMS, pg_vector_db_engine: $TYPE, $$$MORE):
      $$$BODY
"""

# Rule to update pg_vector_db_engine to db_engine for marked tests
REPLACE_PG_VECTOR_ENGINE_RULE = """
id: replace-pg-vector-db-engine
language: python
rule:
  pattern: |
    @pytest.mark.postgres
    async def test_$NAME($$$PARAMS, pg_vector_db_engine: AsyncEngine, $$$MORE):
      $$$BODY
  fix: |
    @pytest.mark.postgres
    async def test_$NAME($$$PARAMS, db_engine: AsyncEngine, $$$MORE):
      $$$BODY
"""


def run_ast_grep(rule_content: str, file_path: Path) -> bool:
    """Run ast-grep with the given rule on a file."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False
    ) as rule_file:
        rule_file.write(rule_content)
        rule_file.flush()

        try:
            result = subprocess.run(
                [
                    "ast-grep",
                    "scan",
                    "--inline-rules",
                    rule_content,
                    "--update-all",
                    str(file_path),
                ],
                capture_output=True,
                text=True,
            )

            if result.returncode != 0 and result.stderr:
                print(f"Error processing {file_path}: {result.stderr}")
                return False

            return True
        finally:
            Path(rule_file.name).unlink()


def find_files_with_pg_vector(root_dir: Path) -> list[Path]:
    """Find all files that use pg_vector_db_engine."""
    result = subprocess.run(
        ["grep", "-r", "-l", "pg_vector_db_engine", str(root_dir), "--include=*.py"],
        capture_output=True,
        text=True,
    )

    if result.returncode == 0 and result.stdout:
        return [Path(f) for f in result.stdout.strip().split("\n") if f]
    return []


def main() -> None:
    # Get the repository root
    script_dir = Path(__file__).parent
    repo_root = script_dir.parent
    tests_dir = repo_root / "tests"

    if not tests_dir.exists():
        print(f"Tests directory not found: {tests_dir}")
        sys.exit(1)

    print("=== PostgreSQL Test Marking Script ===")
    print(f"Repository root: {repo_root}")
    print(f"Tests directory: {tests_dir}")

    # Find all files using pg_vector_db_engine
    files_with_pg_vector = find_files_with_pg_vector(tests_dir)
    print(f"\nFound {len(files_with_pg_vector)} files using pg_vector_db_engine")

    if not files_with_pg_vector:
        print("No files to process.")
        sys.exit(0)

    for file_path in files_with_pg_vector:
        print(f"  - {file_path.relative_to(repo_root)}")

    # Ask for confirmation
    response = input("\nProceed with marking these tests? (y/N): ")
    if response.lower() != "y":
        print("Operation cancelled.")
        sys.exit(0)

    # Process each file
    successful = 0
    failed = 0

    for file_path in files_with_pg_vector:
        print(f"\nProcessing {file_path.relative_to(repo_root)}...")

        # First add the pytest.mark.postgres decorator
        if not run_ast_grep(ADD_POSTGRES_MARK_RULE, file_path):
            failed += 1
            print("  -> Failed to add @pytest.mark.postgres")
            continue

        successful += 1
        print("  -> Success")

    print("\n=== Summary ===")
    print(f"Successfully processed: {successful} files")
    print(f"Failed: {failed} files")

    if failed > 0:
        print("\nSome files failed to process. Please check the errors above.")
        sys.exit(1)

    print("\n=== Next Steps ===")
    print("1. Review the changes with: git diff")
    print("2. Ensure 'import pytest' is present in files with new marks")
    print("3. Run tests with --db postgres to verify PostgreSQL-specific tests")


if __name__ == "__main__":
    main()
