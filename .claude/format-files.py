#!/usr/bin/env python3
"""
File formatter hook for Claude.
Runs configured formatters on files after edit operations.
"""

import fnmatch
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def load_config() -> dict[str, Any] | None:
    """Load formatter configuration."""
    config_path = Path(__file__).parent / "file-formatters.json"
    try:
        with open(config_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading formatter config: {e}", file=sys.stderr)
        return None


def should_exclude(file_path: str, exclude_patterns: list[str]) -> bool:
    """Check if file should be excluded from formatting."""
    return any(fnmatch.fnmatch(file_path, pattern) for pattern in exclude_patterns)


def matches_pattern(file_path: str, patterns: list[str]) -> bool:
    """Check if file matches any of the given patterns."""
    file_name = os.path.basename(file_path)
    return any(fnmatch.fnmatch(file_name, pattern) for pattern in patterns)


def format_file(file_path: str, formatter: dict[str, Any]) -> None:
    """Apply formatter to a file."""
    if not os.path.exists(file_path):
        return

    # Replace placeholder in command with actual file path
    command = formatter["command"].replace("{}", file_path)

    # If command uses sed -i, append the file path
    if "sed -i" in command and file_path not in command:
        command = f"{command} '{file_path}'"

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            print(f"Formatter failed for {file_path}: {result.stderr}", file=sys.stderr)
    except Exception as e:
        print(f"Error formatting {file_path}: {e}", file=sys.stderr)


def main() -> None:
    """Main entry point."""
    # Read the tool use data from stdin
    try:
        tool_data = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        # If we can't parse JSON, exit silently
        return

    # Extract tool name and input
    tool_name = tool_data.get("tool_name", "")
    tool_input = tool_data.get("tool_input", {})

    # We're interested in Edit, MultiEdit, Write, NotebookEdit, and Update tools
    if tool_name not in {"Edit", "MultiEdit", "Write", "NotebookEdit", "Update"}:
        return

    # Load configuration
    config = load_config()
    if not config:
        return

    # Extract file paths from the tool input
    file_paths = []

    if (
        tool_name in {"Edit", "Write", "NotebookEdit", "Update"}
        or tool_name == "MultiEdit"
    ):
        file_path = tool_input.get("file_path")
        if file_path:
            file_paths.append(file_path)

    # Process each file
    for file_path in file_paths:
        # Skip if file should be excluded
        if should_exclude(file_path, config.get("exclude_patterns", [])):
            continue

        # Apply each formatter that matches
        for _formatter_name, formatter in config.get("formatters", {}).items():
            # Skip disabled formatters
            if not formatter.get("enabled", True):
                continue

            # Check if file matches formatter patterns
            if matches_pattern(file_path, formatter.get("patterns", [])):
                format_file(file_path, formatter)


if __name__ == "__main__":
    main()
