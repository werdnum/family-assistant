#!/usr/bin/env python3
"""Build a mapping from source modules to test files that cover them.

Reads a .coverage database generated with ``--cov-context=test`` and produces
``coverage_test_mapping.json`` — a JSON file mapping dotted module prefixes to
lists of test file paths.

Usage::

    # 1. Generate the .coverage database
    pytest --cov=src/family_assistant --cov-context=test -q tests/unit tests/functional

    # 2. Build the mapping
    python scripts/build-coverage-map.py

    # 3. Inspect the output
    python -m json.tool coverage_test_mapping.json
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

from coverage.data import CoverageData


def extract_test_file(context: str) -> str | None:
    """Extract test file path from a coverage context string.

    Context strings look like:
        tests/unit/test_foo.py::TestClass::test_bar|run
        tests/functional/test_baz.py::test_something|run
    """
    match = re.match(r"(tests/\S+\.py)::", context)
    if match:
        return match.group(1)
    return None


def source_file_to_module(filepath: str) -> str:
    """Convert a source file path to a dotted module path.

    ``src/family_assistant/tools/notes.py`` -> ``family_assistant.tools.notes``
    ``/abs/path/src/family_assistant/tools/notes.py`` -> ``family_assistant.tools.notes``
    """
    path = Path(filepath)
    # Make relative to project root if absolute
    with contextlib.suppress(ValueError):
        path = path.relative_to(Path.cwd())
    parts = list(path.parts)
    # Strip src/ prefix
    if parts and parts[0] == "src":
        parts = parts[1:]
    # Strip .py suffix
    if parts and parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    # Drop __init__ from the end
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def module_prefixes(module: str) -> list[str]:
    """Return all prefixes of a dotted module path.

    ``family_assistant.tools.notes`` -> [
        ``family_assistant``,
        ``family_assistant.tools``,
        ``family_assistant.tools.notes``,
    ]
    """
    parts = module.split(".")
    return [".".join(parts[: i + 1]) for i in range(len(parts))]


def build_mapping(coverage_file: str = ".coverage") -> dict[str, list[str]]:
    """Build the source module -> test files mapping."""
    covdata = CoverageData(coverage_file)
    covdata.read()

    # Map: source module -> set of test files
    module_tests: dict[str, set[str]] = defaultdict(set)

    for source_file in covdata.measured_files():
        # Only care about our source code
        if "family_assistant" not in source_file:
            continue

        module = source_file_to_module(source_file)
        if not module.startswith("family_assistant"):
            continue

        # Get all contexts (test functions) that covered lines in this file
        contexts_by_line = covdata.contexts_by_lineno(source_file)

        test_files: set[str] = set()
        for _lineno, contexts in contexts_by_line.items():
            for ctx in contexts:
                test_file = extract_test_file(ctx)
                if test_file:
                    test_files.add(test_file)

        if test_files:
            module_tests[module] |= test_files

    # Aggregate: for each module prefix, collect union of test files
    prefix_tests: dict[str, set[str]] = defaultdict(set)
    for module, tests in module_tests.items():
        for prefix in module_prefixes(module):
            prefix_tests[prefix] |= tests

    # Deduplicate test paths by collapsing to directories where possible
    result: dict[str, list[str]] = {}
    for prefix in sorted(prefix_tests):
        sorted_files: list[str] = sorted(prefix_tests[prefix])
        result[prefix] = _collapse_to_directories(sorted_files)

    return result


def _collapse_to_directories(test_files: list[str]) -> list[str]:
    """Collapse test file paths to directory paths where all files in a dir are present.

    If all .py files in tests/unit/tools/ are in the list, replace them
    with just "tests/unit/tools/".
    """
    # Group by directory
    by_dir: dict[str, list[str]] = defaultdict(list)
    for f in test_files:
        by_dir[str(Path(f).parent)].append(f)

    result: list[str] = []
    for dir_path, files in sorted(by_dir.items()):
        # Check if all python test files in this directory are covered
        dir_p = Path(dir_path)
        if dir_p.exists():
            all_test_files = sorted(
                str(p) for p in dir_p.glob("test_*.py") if p.is_file()
            )
            if all_test_files and set(all_test_files) <= set(files):
                result.append(str(dir_path) + "/")
                continue
        result.extend(sorted(files))

    return result


def main() -> None:
    coverage_file = os.environ.get("COVERAGE_FILE", ".coverage")
    output_file = "coverage_test_mapping.json"

    if not Path(coverage_file).exists():
        print(f"Error: Coverage file '{coverage_file}' not found.", file=sys.stderr)
        print("Run tests with --cov-context=test first:", file=sys.stderr)
        print(
            "  pytest --cov=src/family_assistant --cov-context=test -q tests/",
            file=sys.stderr,
        )
        sys.exit(1)

    mapping = build_mapping(coverage_file)

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2, sort_keys=True)

    print(f"Written {output_file} with {len(mapping)} module entries")
    # Print summary
    for key in sorted(mapping):
        depth = key.count(".")
        if depth <= 1:  # Only show top-level and first nesting
            test_count = len(mapping[key])
            print(f"  {key}: {test_count} test paths")


if __name__ == "__main__":
    main()
