import os
from pathlib import Path

# Get the absolute path of the project root directory
PROJECT_ROOT = Path(os.getcwd()).resolve()


def list_source_files(path: str = ".") -> str:
    """
    Lists files and directories recursively within the project.

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

        items = []
        for item in sorted(absolute_path.iterdir()):
            items.append(f"{item.name}{'/' if item.is_dir() else ''}")
        return "\n".join(items)

    except Exception as e:
        return f"An unexpected error occurred: {e}"


def read_file_chunk(file_path: str, start_line: int, end_line: int) -> str:
    """
    Reads a specific range of lines from a file.

    Args:
        file_path: The relative path to the file from the project root.
        start_line: The starting line number (1-indexed).
        end_line: The ending line number.

    Returns:
        The content of the file as a string, or an error message if the file
        cannot be accessed.
    """
    try:
        absolute_file_path = PROJECT_ROOT.joinpath(file_path).resolve()
        if PROJECT_ROOT not in absolute_file_path.parents and absolute_file_path != PROJECT_ROOT:
            return "Error: Access denied. File is outside the project directory."

        if not absolute_file_path.is_file():
            return f"Error: File not found at '{file_path}'"

        with open(absolute_file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        start_index = max(0, start_line - 1)
        end_index = min(len(lines), end_line)

        return "".join(lines[start_index:end_index])

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
        if PROJECT_ROOT not in absolute_file_path.parents and absolute_file_path != PROJECT_ROOT:
            return "Error: Access denied. File is outside the project directory."

        if not absolute_file_path.is_file():
            return f"Error: File not found at '{file_path}'"

        results = []
        with open(absolute_file_path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if search_string in line:
                    results.append(f"{i+1}: {line.strip()}")
        return "\n".join(results)

    except Exception as e:
        return f"An unexpected error occurred: {e}"


SOURCE_READER_TOOLS_DEFINITION = [
    {
        "type": "function",
        "function": {
            "name": "list_source_files",
            "description": "Lists files and directories recursively within the project.",
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
            "description": "Reads a specific range of lines from a file.",
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
            "description": "Searches for a specific string within a file and returns the line number and content.",
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
