import os
from pathlib import Path

# Get the absolute path of the project root directory
PROJECT_ROOT = Path(os.getcwd()).resolve()


def read_source_code(file_path: str) -> str:
    """
    Reads the content of a specified source code file from the project directory.

    This tool is intended for debugging and reviewing the application's source code.
    For security reasons, it can only access files within the project's directory.

    Args:
        file_path: The relative path to the file from the project root.

    Returns:
        The content of the file as a string, or an error message if the file
        cannot be accessed.
    """
    try:
        # Resolve the absolute path of the requested file
        absolute_file_path = PROJECT_ROOT.joinpath(file_path).resolve()

        # Security check: Ensure the resolved path is within the project directory
        if PROJECT_ROOT not in absolute_file_path.parents and absolute_file_path != PROJECT_ROOT:
            return "Error: Access denied. File is outside the project directory."

        if not absolute_file_path.is_file():
            return f"Error: File not found at '{file_path}'"

        return absolute_file_path.read_text(encoding="utf-8")

    except Exception as e:
        return f"An unexpected error occurred: {e}"


SOURCE_CODE_TOOLS_DEFINITION = [
    {
        "type": "function",
        "function": {
            "name": "read_source_code",
            "description": "Reads the content of a specified source code file from the project directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The relative path to the file from the project root.",
                    },
                },
                "required": ["file_path"],
            },
        },
    }
]
