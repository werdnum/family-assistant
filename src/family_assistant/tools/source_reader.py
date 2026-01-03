"""Source code reading tools for the engineer profile."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _get_project_root() -> Path:
    """
    Get the project root directory.

    Uses PROJECT_ROOT environment variable if set, otherwise traverses
    up from the current file's location to find a marker file
    (pyproject.toml, .git, or config.yaml).

    Returns:
        The absolute path to the project root directory.
    """
    # First, check for environment variable (preferred for production/containers)
    env_root = os.environ.get("PROJECT_ROOT")
    if env_root:
        return Path(env_root).resolve()

    # Fall back to finding marker files by traversing up from this file's location
    current = Path(__file__).resolve().parent
    markers = ["pyproject.toml", ".git", "config.yaml"]

    while current != current.parent:
        for marker in markers:
            if (current / marker).exists():
                return current
        current = current.parent

    # If no marker found, fall back to cwd (least reliable)
    return Path.cwd().resolve()


# Cache the project root
PROJECT_ROOT = _get_project_root()


def list_source_files(path: str = ".") -> str:
    """
    Lists files and directories within the specified path in the project.

    Note: This function lists only the immediate contents of the directory,
    not nested subdirectories. Use it iteratively to explore deeper.

    Args:
        path: The relative path to the directory from the project root.

    Returns:
        A string containing a list of files and directories.
    """
    try:
        absolute_path = PROJECT_ROOT.joinpath(path).resolve()
        if PROJECT_ROOT not in absolute_path.parents and absolute_path != PROJECT_ROOT:
            return "Error: Access denied. Path is outside the project directory."

        if not absolute_path.exists():
            return f"Error: Path not found at '{path}'"

        if not absolute_path.is_dir():
            return f"Error: '{path}' is not a directory."

        items = []
        for item in sorted(absolute_path.iterdir()):
            items.append(f"{item.name}{'/' if item.is_dir() else ''}")
        return "\n".join(items) if items else "(empty directory)"

    except Exception as e:
        return f"An unexpected error occurred: {e}"


def read_file_chunk(file_path: str, start_line: int, end_line: int) -> str:
    """
    Reads a specific range of lines from a file efficiently.

    This function reads lines incrementally rather than loading the entire file,
    making it suitable for large files.

    Args:
        file_path: The relative path to the file from the project root.
        start_line: The starting line number (1-indexed).
        end_line: The ending line number.

    Returns:
        The content of the requested lines, or an error message if the file
        cannot be accessed.
    """
    try:
        absolute_file_path = PROJECT_ROOT.joinpath(file_path).resolve()
        if (
            PROJECT_ROOT not in absolute_file_path.parents
            and absolute_file_path != PROJECT_ROOT
        ):
            return "Error: Access denied. File is outside the project directory."

        if not absolute_file_path.is_file():
            return f"Error: File not found at '{file_path}'"

        # Validate line numbers
        if start_line < 1:
            return "Error: start_line must be at least 1."
        if end_line < start_line:
            return "Error: end_line must be >= start_line."

        # Read lines efficiently without loading entire file into memory
        result_lines: list[str] = []
        with open(absolute_file_path, encoding="utf-8") as f:
            for i, line in enumerate(f, start=1):
                if i > end_line:
                    break
                if i >= start_line:
                    result_lines.append(line)

        if not result_lines:
            return f"Error: No lines in range {start_line}-{end_line} (file may be shorter)."

        return "".join(result_lines)

    except UnicodeDecodeError:
        return f"Error: Cannot read '{file_path}' - file is not valid UTF-8 text."
    except Exception as e:
        return f"An unexpected error occurred: {e}"


def search_in_file(file_path: str, search_string: str) -> str:
    """
    Searches for a specific string within a file and returns the line number and content.

    Args:
        file_path: The relative path to the file from the project root.
        search_string: The string to search for.

    Returns:
        A string containing the line number and content of the matching lines.
    """
    try:
        absolute_file_path = PROJECT_ROOT.joinpath(file_path).resolve()
        if (
            PROJECT_ROOT not in absolute_file_path.parents
            and absolute_file_path != PROJECT_ROOT
        ):
            return "Error: Access denied. File is outside the project directory."

        if not absolute_file_path.is_file():
            return f"Error: File not found at '{file_path}'"

        results = []
        with open(absolute_file_path, encoding="utf-8") as f:
            for i, line in enumerate(f, start=1):
                if search_string in line:
                    content = line.rstrip()
                    if len(content) > 500:
                        content = content[:500] + "... (truncated)"
                    results.append(f"{i}: {content}")
        return "\n".join(results) if results else "No matches found."

    except UnicodeDecodeError:
        return f"Error: Cannot read '{file_path}' - file is not valid UTF-8 text."
    except Exception as e:
        return f"An unexpected error occurred: {e}"


# ast-grep-ignore: no-dict-any - Legacy tool definition format
SOURCE_READER_TOOLS_DEFINITION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_source_files",
            "description": (
                "Lists files and directories within the specified path in the project. "
                "Returns immediate contents only; use iteratively to explore subdirectories."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The relative path to the directory from the project root.",
                        "default": ".",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file_chunk",
            "description": "Reads a specific range of lines from a file efficiently.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The relative path to the file from the project root.",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "The starting line number (1-indexed).",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "The ending line number.",
                    },
                },
                "required": ["file_path", "start_line", "end_line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_in_file",
            "description": "Searches for a specific string within a file and returns matching line numbers and content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The relative path to the file from the project root.",
                    },
                    "search_string": {
                        "type": "string",
                        "description": "The string to search for.",
                    },
                },
                "required": ["file_path", "search_string"],
            },
        },
    },
]
