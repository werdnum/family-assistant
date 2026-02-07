"""Workspace file tools for AI worker sandbox.

This module provides tools for reading, writing, and managing files within
the workspace directory. All paths are relative to the workspace root and
validated to prevent directory traversal attacks.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

import aiofiles
import aiofiles.os
import yaml

from family_assistant.tools.types import ToolResult
from family_assistant.utils.workspace import get_workspace_root, validate_workspace_path

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolDefinition, ToolExecutionContext

logger = logging.getLogger(__name__)


# Tool Definitions
_WORKSPACE_FILE_TOOLS: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "workspace_read",
            "description": (
                "Read file contents from the workspace directory. "
                "All paths are relative to the workspace root. "
                "Use this to read files from the shared workspace, task outputs, or attachments.\n\n"
                "Returns: On success, returns the file contents (text or base64 for binary). "
                "On file not found, returns 'Error: File not found: [path]'. "
                "On invalid path, returns 'Error: Invalid path: [details]'. "
                "On read error, returns 'Error: Failed to read file: [details]'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Relative path to the file within the workspace "
                            "(e.g., 'shared/data/input.csv', 'tasks/task-123/output/report.md')"
                        ),
                    },
                    "offset": {
                        "type": "integer",
                        "description": (
                            "Optional. Line number to start reading from (1-indexed). "
                            "Use for reading large files in chunks."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Optional. Maximum number of lines to read. "
                            "Use for reading large files in chunks."
                        ),
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_write",
            "description": (
                "Write content to a file in the workspace directory. "
                "Creates parent directories if they don't exist. "
                "All paths are relative to the workspace root.\n\n"
                "Returns: On success, returns 'OK. File written: [path] ([size] bytes)'. "
                "On invalid path, returns 'Error: Invalid path: [details]'. "
                "On write error, returns 'Error: Failed to write file: [details]'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Relative path for the file within the workspace "
                            "(e.g., 'shared/scripts/process.py', 'tasks/task-123/input/data.json')"
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "The content to write to the file.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_glob",
            "description": (
                "Find files matching a glob pattern in the workspace. "
                "Also serves as a list/exists check for files and directories. "
                "All paths are relative to the workspace root.\n\n"
                "Returns: A list of matching file paths with optional metadata. "
                "On invalid path, returns 'Error: Invalid path: [details]'. "
                "On error, returns 'Error: Failed to search: [details]'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": (
                            "Glob pattern to match files (e.g., '*.py', '**/*.csv', 'shared/data/*'). "
                            "Use '*' to list directory contents, '**/*.ext' for recursive search."
                        ),
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "Optional. Base directory for the search, relative to workspace root. "
                            "Defaults to workspace root."
                        ),
                    },
                    "include_info": {
                        "type": "boolean",
                        "description": (
                            "Optional. If true, includes file size and modification time. "
                            "Useful for checking if a file exists."
                        ),
                        "default": False,
                    },
                    "max_results": {
                        "type": "integer",
                        "description": (
                            "Optional. Maximum number of results to return. "
                            "Defaults to 1000 to prevent performance issues with broad patterns."
                        ),
                        "default": 1000,
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_delete",
            "description": (
                "Delete a file or empty directory from the workspace. "
                "All paths are relative to the workspace root.\n\n"
                "Returns: On success, returns 'OK. Deleted: [path]'. "
                "On file not found, returns 'Error: Path not found: [path]'. "
                "On non-empty directory, returns 'Error: Directory not empty: [path]'. "
                "On invalid path, returns 'Error: Invalid path: [details]'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Relative path to the file or empty directory to delete."
                        ),
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_mkdir",
            "description": (
                "Create a directory in the workspace (with parent directories if needed). "
                "All paths are relative to the workspace root.\n\n"
                "Returns: On success, returns 'OK. Directory created: [path]'. "
                "On already exists, returns 'OK. Directory already exists: [path]'. "
                "On invalid path, returns 'Error: Invalid path: [details]'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Relative path for the directory to create "
                            "(e.g., 'tasks/task-123/output', 'shared/data/processed')"
                        ),
                    },
                },
                "required": ["path"],
            },
        },
    },
]


async def workspace_read_tool(
    exec_context: ToolExecutionContext,
    path: str,
    offset: int | None = None,
    limit: int | None = None,
) -> ToolResult:
    """Read file contents from the workspace directory.

    Args:
        exec_context: The tool execution context
        path: Relative path to the file within workspace
        offset: Optional line number to start from (1-indexed)
        limit: Optional maximum number of lines to read

    Returns:
        ToolResult with file contents or error message
    """
    logger.info(f"workspace_read: path={path}, offset={offset}, limit={limit}")

    try:
        workspace_root = get_workspace_root(exec_context)
        full_path = validate_workspace_path(path, workspace_root)
    except ValueError as e:
        return ToolResult(data={"error": str(e)})

    if not full_path.exists():
        return ToolResult(data={"error": f"File not found: {path}"})

    if full_path.is_dir():
        return ToolResult(data={"error": f"Path is a directory, not a file: {path}"})

    try:
        async with aiofiles.open(full_path, encoding="utf-8") as f:
            if offset is not None or limit is not None:
                # Read specific lines efficiently by iterating
                # Note: We don't count total lines as that would require
                # reading the entire file, defeating the memory optimization
                start_idx = (offset - 1) if offset else 0
                max_lines = limit if limit else float("inf")
                selected_lines: list[str] = []
                line_count = 0
                has_more = False

                async for line in f:
                    if line_count >= start_idx:
                        if len(selected_lines) >= max_lines:
                            # We've hit the limit; there's definitely more
                            has_more = True
                            break
                        selected_lines.append(line)
                    line_count += 1

                content = "".join(selected_lines)
                return ToolResult(
                    data={
                        "path": path,
                        "content": content,
                        "lines_returned": len(selected_lines),
                        "offset": offset or 1,
                        "has_more": has_more,
                    }
                )
            else:
                content = await f.read()
                return ToolResult(data={"path": path, "content": content})
    except UnicodeDecodeError:
        # Try reading as binary and return base64
        async with aiofiles.open(full_path, mode="rb") as f:
            binary_content = await f.read()
            encoded = base64.b64encode(binary_content).decode("ascii")
            return ToolResult(
                data={
                    "path": path,
                    "content_base64": encoded,
                    "size": len(binary_content),
                    "encoding": "base64",
                }
            )
    except Exception as e:
        logger.error(f"Failed to read file {path}: {e}", exc_info=True)
        return ToolResult(data={"error": f"Failed to read file: {e}"})


async def workspace_write_tool(
    exec_context: ToolExecutionContext,
    path: str,
    content: str,
) -> ToolResult:
    """Write content to a file in the workspace directory.

    Args:
        exec_context: The tool execution context
        path: Relative path for the file within workspace
        content: The content to write

    Returns:
        ToolResult with success message or error
    """
    logger.info(f"workspace_write: path={path}, content_length={len(content)}")

    try:
        workspace_root = get_workspace_root(exec_context)
        full_path = validate_workspace_path(path, workspace_root)
    except ValueError as e:
        return ToolResult(data={"error": str(e)})

    try:
        # Create parent directories if needed
        await aiofiles.os.makedirs(full_path.parent, exist_ok=True)

        async with aiofiles.open(full_path, mode="w", encoding="utf-8") as f:
            await f.write(content)

        size = len(content.encode("utf-8"))
        logger.info(f"Wrote {size} bytes to {path}")
        return ToolResult(
            text=f"OK. File written: {path} ({size} bytes)",
            data={"path": path, "size": size, "success": True},
        )
    except Exception as e:
        logger.error(f"Failed to write file {path}: {e}", exc_info=True)
        return ToolResult(data={"error": f"Failed to write file: {e}"})


async def workspace_glob_tool(
    exec_context: ToolExecutionContext,
    pattern: str,
    path: str | None = None,
    include_info: bool = False,
    max_results: int = 1000,
) -> ToolResult:
    """Find files matching a glob pattern in the workspace.

    Args:
        exec_context: The tool execution context
        pattern: Glob pattern to match files
        path: Optional base directory for the search
        include_info: Whether to include file size and modification time
        max_results: Maximum number of results to return (default 1000)

    Returns:
        ToolResult with list of matching files
    """
    logger.info(
        f"workspace_glob: pattern={pattern}, path={path}, include_info={include_info}, "
        f"max_results={max_results}"
    )

    try:
        workspace_root = get_workspace_root(exec_context)
        base_path = (
            validate_workspace_path(path, workspace_root) if path else workspace_root
        )
    except ValueError as e:
        return ToolResult(data={"error": str(e)})

    if not base_path.exists():
        return ToolResult(data={"error": f"Path not found: {path or '.'}"})

    try:
        # ast-grep-ignore: no-dict-any - Dynamic file metadata structure
        matches: list[dict[str, Any]] = []
        truncated = False

        # pathlib.glob natively supports ** for recursive patterns
        for match_path in base_path.glob(pattern):
            if len(matches) >= max_results:
                truncated = True
                break
            rel_path = str(match_path.relative_to(workspace_root))
            if include_info:
                stat = match_path.stat()
                matches.append({
                    "path": rel_path,
                    "is_file": match_path.is_file(),
                    "size": stat.st_size if match_path.is_file() else None,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                })
            else:
                matches.append({"path": rel_path, "is_file": match_path.is_file()})

        return ToolResult(
            data={
                "pattern": pattern,
                "base_path": path or ".",
                "matches": matches,
                "count": len(matches),
                "truncated": truncated,
            }
        )
    except Exception as e:
        logger.error(f"Failed to search pattern {pattern}: {e}", exc_info=True)
        return ToolResult(data={"error": f"Failed to search: {e}"})


async def workspace_delete_tool(
    exec_context: ToolExecutionContext,
    path: str,
) -> ToolResult:
    """Delete a file or empty directory from the workspace.

    Args:
        exec_context: The tool execution context
        path: Relative path to the file or directory to delete

    Returns:
        ToolResult with success message or error
    """
    logger.info(f"workspace_delete: path={path}")

    try:
        workspace_root = get_workspace_root(exec_context)
        full_path = validate_workspace_path(path, workspace_root)
    except ValueError as e:
        return ToolResult(data={"error": str(e)})

    if not full_path.exists():
        return ToolResult(data={"error": f"Path not found: {path}"})

    try:
        if full_path.is_file():
            await aiofiles.os.remove(full_path)
            logger.info(f"Deleted file: {path}")
            return ToolResult(
                text=f"OK. Deleted: {path}",
                data={"path": path, "type": "file", "deleted": True},
            )
        elif full_path.is_dir():
            # Attempt to remove directory - rmdir will fail if not empty
            # This avoids TOCTOU race conditions from checking then removing
            try:
                await aiofiles.os.rmdir(full_path)
                logger.info(f"Deleted empty directory: {path}")
                return ToolResult(
                    text=f"OK. Deleted: {path}",
                    data={"path": path, "type": "directory", "deleted": True},
                )
            except OSError as dir_err:
                # Directory not empty or other error
                contents = list(full_path.iterdir())
                if contents:
                    return ToolResult(
                        data={
                            "error": f"Directory not empty: {path}",
                            "item_count": len(contents),
                        }
                    )
                # Re-raise if it was a different error
                raise dir_err
        else:
            return ToolResult(data={"error": f"Unknown path type: {path}"})
    except Exception as e:
        logger.error(f"Failed to delete {path}: {e}", exc_info=True)
        return ToolResult(data={"error": f"Failed to delete: {e}"})


async def workspace_mkdir_tool(
    exec_context: ToolExecutionContext,
    path: str,
) -> ToolResult:
    """Create a directory in the workspace.

    Args:
        exec_context: The tool execution context
        path: Relative path for the directory to create

    Returns:
        ToolResult with success message or error
    """
    logger.info(f"workspace_mkdir: path={path}")

    try:
        workspace_root = get_workspace_root(exec_context)
        full_path = validate_workspace_path(path, workspace_root)
    except ValueError as e:
        return ToolResult(data={"error": str(e)})

    try:
        if full_path.exists():
            if full_path.is_dir():
                return ToolResult(
                    text=f"OK. Directory already exists: {path}",
                    data={"path": path, "already_exists": True},
                )
            else:
                return ToolResult(
                    data={"error": f"Path exists but is not a directory: {path}"}
                )

        await aiofiles.os.makedirs(full_path, exist_ok=True)
        logger.info(f"Created directory: {path}")
        return ToolResult(
            text=f"OK. Directory created: {path}",
            data={"path": path, "created": True},
        )
    except Exception as e:
        logger.error(f"Failed to create directory {path}: {e}", exc_info=True)
        return ToolResult(data={"error": f"Failed to create directory: {e}"})


# Notes Integration Tool Definitions
NOTES_INTEGRATION_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "workspace_export_notes",
            "description": (
                "Export notes to workspace files as markdown with YAML frontmatter. "
                "Use this to prepare notes for worker tasks or backup.\n\n"
                "Returns: List of exported file paths with metadata. "
                "On error, returns 'Error: [details]'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "dest_dir": {
                        "type": "string",
                        "description": (
                            "Destination directory relative to workspace root "
                            "(e.g., 'shared/notes', 'tasks/task-123/context')"
                        ),
                    },
                    "titles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional. List of specific note titles to export. "
                            "If not provided, exports all notes."
                        ),
                    },
                    "max_notes": {
                        "type": "integer",
                        "description": (
                            "Optional. Maximum number of notes to export. "
                            "Defaults to 50."
                        ),
                        "default": 50,
                    },
                },
                "required": ["dest_dir"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_import_note",
            "description": (
                "Import a workspace file as a note. "
                "Supports markdown files with optional YAML frontmatter.\n\n"
                "Returns: On success, returns details of the created/updated note. "
                "On error, returns 'Error: [details]'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Relative path to the file within workspace "
                            "(e.g., 'tasks/task-123/output/summary.md')"
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": (
                            "Optional. Title for the note. "
                            "If not provided, uses the filename without extension."
                        ),
                    },
                    "include_in_prompt": {
                        "type": "boolean",
                        "description": (
                            "Optional. Whether to include this note in system prompts. "
                            "Takes precedence over file frontmatter; defaults to true "
                            "if neither specified."
                        ),
                    },
                },
                "required": ["path"],
            },
        },
    },
]


async def workspace_export_notes_tool(
    exec_context: ToolExecutionContext,
    dest_dir: str,
    titles: list[str] | None = None,
    max_notes: int = 50,
) -> ToolResult:
    """Export notes to workspace files as markdown with YAML frontmatter.

    Args:
        exec_context: The tool execution context
        dest_dir: Destination directory relative to workspace root
        titles: Optional list of specific note titles to export
        max_notes: Maximum number of notes to export

    Returns:
        ToolResult with list of exported files
    """
    logger.info(
        f"workspace_export_notes: dest_dir={dest_dir}, titles={titles}, max_notes={max_notes}"
    )

    try:
        workspace_root = get_workspace_root(exec_context)
        dest_path = validate_workspace_path(dest_dir, workspace_root)
    except ValueError as e:
        return ToolResult(data={"error": str(e)})

    db_context = exec_context.db_context

    try:
        # Create destination directory if needed
        await aiofiles.os.makedirs(dest_path, exist_ok=True)

        # Get notes from database
        all_notes = await db_context.notes.get_all(
            visibility_grants=exec_context.visibility_grants
        )

        # Filter by titles if specified
        if titles:
            notes_to_export = [n for n in all_notes if n.title in titles]
        else:
            notes_to_export = all_notes[:max_notes]

        # Export each note
        # ast-grep-ignore: no-dict-any - Dynamic export metadata structure
        exported: list[dict[str, Any]] = []
        used_filenames: set[str] = set()
        for note in notes_to_export:
            title = note.title
            content = note.content
            include_in_prompt = note.include_in_prompt

            # Create safe filename from title
            safe_filename = "".join(
                c if c.isalnum() or c in {"-", "_", " "} else "_" for c in title
            )
            safe_filename = safe_filename.strip().replace(" ", "_")[:100]

            # Handle filename collisions by appending a counter
            base_filename = safe_filename
            counter = 1
            while safe_filename in used_filenames:
                safe_filename = f"{base_filename}_{counter}"
                counter += 1
            used_filenames.add(safe_filename)

            file_path = dest_path / f"{safe_filename}.md"

            # Format as markdown with YAML frontmatter
            frontmatter = f"""---
title: {title}
include_in_prompt: {str(include_in_prompt).lower()}
---

"""
            file_content = frontmatter + content

            # Write file
            async with aiofiles.open(file_path, mode="w", encoding="utf-8") as f:
                await f.write(file_content)

            rel_path = str(file_path.relative_to(workspace_root))
            exported.append({
                "path": rel_path,
                "title": title,
                "size": len(file_content.encode("utf-8")),
            })

        logger.info(f"Exported {len(exported)} notes to {dest_dir}")
        return ToolResult(
            text=f"OK. Exported {len(exported)} notes to {dest_dir}",
            data={
                "dest_dir": dest_dir,
                "exported": exported,
                "count": len(exported),
            },
        )
    except Exception as e:
        logger.error(f"Failed to export notes: {e}", exc_info=True)
        return ToolResult(data={"error": f"Failed to export notes: {e}"})


async def workspace_import_note_tool(
    exec_context: ToolExecutionContext,
    path: str,
    title: str | None = None,
    include_in_prompt: bool | None = None,
) -> ToolResult:
    """Import a workspace file as a note.

    Args:
        exec_context: The tool execution context
        path: Relative path to the file within workspace
        title: Optional title for the note (defaults to filename)
        include_in_prompt: Whether to include in system prompts (default True,
            but frontmatter value takes precedence if not explicitly specified)

    Returns:
        ToolResult with import details
    """
    logger.info(
        f"workspace_import_note: path={path}, title={title}, include_in_prompt={include_in_prompt}"
    )

    try:
        workspace_root = get_workspace_root(exec_context)
        full_path = validate_workspace_path(path, workspace_root)
    except ValueError as e:
        return ToolResult(data={"error": str(e)})

    if not full_path.exists():
        return ToolResult(data={"error": f"File not found: {path}"})

    if full_path.is_dir():
        return ToolResult(data={"error": f"Path is a directory, not a file: {path}"})

    db_context = exec_context.db_context

    try:
        # Read file content
        async with aiofiles.open(full_path, encoding="utf-8") as f:
            file_content = await f.read()

        # Parse YAML frontmatter if present
        note_title = title
        note_include_in_prompt = include_in_prompt  # May be None
        content = file_content

        if file_content.startswith("---"):
            # Try to parse frontmatter using proper YAML parsing
            parts = file_content.split("---", 2)
            if len(parts) >= 3:
                frontmatter_text = parts[1].strip()
                content = parts[2].strip()

                # Parse YAML frontmatter properly
                try:
                    frontmatter = yaml.safe_load(frontmatter_text)
                    if isinstance(frontmatter, dict):
                        # Only use frontmatter values if function args weren't specified
                        if "title" in frontmatter and not title:
                            note_title = str(frontmatter["title"])
                        if (
                            "include_in_prompt" in frontmatter
                            and include_in_prompt is None
                        ):
                            note_include_in_prompt = bool(
                                frontmatter["include_in_prompt"]
                            )
                except yaml.YAMLError:
                    # If YAML parsing fails, just use the content as-is
                    logger.warning(f"Failed to parse YAML frontmatter in {path}")

        # Default to True if not specified anywhere
        if note_include_in_prompt is None:
            note_include_in_prompt = True

        # Use filename as title if not specified
        if not note_title:
            note_title = full_path.stem  # filename without extension

        # Create/update the note
        await db_context.notes.add_or_update(
            title=note_title,
            content=content,
            include_in_prompt=note_include_in_prompt,
        )

        logger.info(f"Imported note '{note_title}' from {path}")
        return ToolResult(
            text=f"OK. Imported note '{note_title}' from {path}",
            data={
                "path": path,
                "title": note_title,
                "include_in_prompt": note_include_in_prompt,
                "content_length": len(content),
            },
        )
    except Exception as e:
        logger.error(f"Failed to import note from {path}: {e}", exc_info=True)
        return ToolResult(data={"error": f"Failed to import note: {e}"})


# Combined tool definitions for export
WORKSPACE_TOOLS_DEFINITION: list[ToolDefinition] = (
    _WORKSPACE_FILE_TOOLS + NOTES_INTEGRATION_TOOLS_DEFINITION
)
