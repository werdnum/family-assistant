#!/usr/bin/env python3
"""
Simple migration script to add db_engine parameter to test functions.

This uses regex-based replacement which is less sophisticated than AST-based
but more practical for our use case.
"""

import re
import sys
from pathlib import Path
from typing import tuple

# Patterns for migration
PATTERNS = [
    # 1. Add db_engine to tests without parameters
    (r"(async def test_\w+)\(\)(\s*)(->.*?)?:", r"\1(db_engine: AsyncEngine)\2\3:"),
    # 2. Add db_engine to tests with parameters but no db_engine/test_db_engine
    (
        r"(async def test_\w+\()([^)]+)(\)\s*(?:->.*?)?\s*:)",
        lambda m: m.group(1) + m.group(2) + ", db_engine: AsyncEngine" + m.group(3)
        if "db_engine" not in m.group(2) and "test_db_engine" not in m.group(2)
        else m.group(0),
    ),
    # 3. Replace test_db_engine with db_engine
    (r"\btest_db_engine\b(\s*:\s*AsyncEngine)", r"db_engine\1"),
    # 4. Replace test_db_engine references in function bodies
    (r"\btest_db_engine\b(?!\s*:)", r"db_engine"),
]


def migrate_file(file_path: Path, dry_run: bool = False) -> tuple[bool, str]:
    """
    Migrate a single file.

    Returns:
        (success, message)
    """
    try:
        content = file_path.read_text()
        original_content = content

        # Check if we need to add import
        if (
            "AsyncEngine" in content
            and "from sqlalchemy.ext.asyncio import AsyncEngine" not in content
        ):
            # Find where to add import (after other imports)
            import_match = re.search(r"((?:from|import)\s+.*\n)+", content)
            if import_match:
                end_pos = import_match.end()
                content = (
                    content[:end_pos]
                    + "from sqlalchemy.ext.asyncio import AsyncEngine\n"
                    + content[end_pos:]
                )

        # Apply patterns
        for pattern, replacement in PATTERNS:
            if callable(replacement):
                content = re.sub(pattern, replacement, content)
            else:
                content = re.sub(pattern, replacement, content)

        if content != original_content:
            if not dry_run:
                file_path.write_text(content)
            return True, "Modified"
        else:
            return True, "No changes needed"

    except Exception as e:
        return False, f"Error: {e}"


def main() -> int:
    """Main migration script."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Migrate tests to use db_engine fixture"
    )
    parser.add_argument("paths", nargs="+", help="Files or directories to migrate")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without modifying files",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show detailed output"
    )

    args = parser.parse_args()

    # Collect all test files
    test_files = []
    for path_str in args.paths:
        path = Path(path_str)
        if path.is_file():
            test_files.append(path)
        elif path.is_dir():
            test_files.extend(path.rglob("test_*.py"))

    if not test_files:
        print("No test files found")
        return 1

    print(f"Found {len(test_files)} test files")
    if args.dry_run:
        print("DRY RUN - no files will be modified")

    # Process files
    modified_count = 0
    error_count = 0

    for test_file in test_files:
        if test_file.name == "conftest.py":
            continue

        success, message = migrate_file(test_file, args.dry_run)

        if success and message == "Modified":
            modified_count += 1
            if args.verbose:
                print(f"✓ {test_file}: {message}")
        elif not success:
            error_count += 1
            print(f"✗ {test_file}: {message}")
        elif args.verbose:
            print(f"- {test_file}: {message}")

    print("\nSummary:")
    print(f"  Modified: {modified_count} files")
    print(f"  Errors: {error_count} files")
    print(f"  Unchanged: {len(test_files) - modified_count - error_count} files")

    if args.dry_run and modified_count > 0:
        print("\nRun without --dry-run to apply changes")

    return 0 if error_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
