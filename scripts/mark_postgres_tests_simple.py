#!/usr/bin/env python3
"""
Simple script to mark tests that use pg_vector_db_engine with @pytest.mark.postgres.
"""

import re
import sys
from pathlib import Path


def mark_postgres_tests(file_path: Path) -> bool:
    """Mark tests using pg_vector_db_engine with @pytest.mark.postgres."""
    try:
        content = file_path.read_text()
        original_content = content

        # Pattern to find test functions using pg_vector_db_engine
        pattern = r"((?:^|\n)((?:    )*)async def test_\w+\([^)]*pg_vector_db_engine[^)]*\)(?:\s*->.*?)?:\n)"

        # Find all matches
        matches = list(re.finditer(pattern, content, re.MULTILINE))

        if not matches:
            return False

        # Process matches in reverse to maintain positions
        for match in reversed(matches):
            indent = match.group(2)
            start_pos = match.start(1)

            # Check if already marked
            check_start = max(0, start_pos - 100)
            check_text = content[check_start:start_pos]
            if "@pytest.mark.postgres" in check_text:
                continue

            # Add the marker
            marker = f"\n{indent}@pytest.mark.postgres"
            content = content[:start_pos] + marker + content[start_pos:]

        # Add import if needed
        if content != original_content and "import pytest" not in content:
            # Find where to add import
            import_match = re.search(
                r"^((?:from|import)\s+.*\n)+", content, re.MULTILINE
            )
            if import_match:
                end_pos = import_match.end()
                content = content[:end_pos] + "import pytest\n" + content[end_pos:]
            else:
                content = "import pytest\n\n" + content

        if content != original_content:
            file_path.write_text(content)
            return True

        return False

    except Exception as e:
        print(f"Error processing {file_path}: {e}")
        return False


def main() -> None:
    # Get the repository root
    script_dir = Path(__file__).parent
    repo_root = script_dir.parent
    tests_dir = repo_root / "tests"

    if not tests_dir.exists():
        print(f"Tests directory not found: {tests_dir}")
        sys.exit(1)

    print("=== PostgreSQL Test Marking Script (Simple) ===")
    print(f"Repository root: {repo_root}")
    print(f"Tests directory: {tests_dir}")

    # Find all Python test files
    test_files = list(tests_dir.rglob("test_*.py"))

    # Filter files that use pg_vector_db_engine
    files_to_process = []
    for file_path in test_files:
        try:
            content = file_path.read_text()
            if "pg_vector_db_engine" in content:
                files_to_process.append(file_path)
        except Exception:
            pass

    print(f"\nFound {len(files_to_process)} files using pg_vector_db_engine")

    if not files_to_process:
        print("No files to process.")
        sys.exit(0)

    for file_path in files_to_process:
        print(f"  - {file_path.relative_to(repo_root)}")

    # Ask for confirmation
    response = input("\nProceed with marking these tests? (y/N): ")
    if response.lower() != "y":
        print("Operation cancelled.")
        sys.exit(0)

    # Process each file
    successful = 0
    failed = 0

    for file_path in files_to_process:
        print(f"\nProcessing {file_path.relative_to(repo_root)}...", end="")

        if mark_postgres_tests(file_path):
            successful += 1
            print(" ✓")
        else:
            failed += 1
            print(" ✗")

    print("\n=== Summary ===")
    print(f"Successfully processed: {successful} files")
    print(f"Failed: {failed} files")
    print(
        f"Already marked/skipped: {len(files_to_process) - successful - failed} files"
    )

    if successful > 0:
        print("\n=== Next Steps ===")
        print("1. Review the changes with: git diff")
        print("2. Run tests with --db postgres to verify PostgreSQL-specific tests")
        print("3. Commit the changes")


if __name__ == "__main__":
    main()
