"""Engineering tools for debugging and diagnosing the application.

Provides read-only access to source code, database queries, error logs,
and GitHub issue creation for the engineer processing profile.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

import aiofiles
import httpx
import sqlparse
from sqlalchemy import text

from family_assistant.paths import PROJECT_ROOT
from family_assistant.tools.types import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from pathlib import Path

    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

# Maximum rows returned from database queries
_MAX_QUERY_ROWS = 1000

# Maximum file size to read (10 MB)
_MAX_FILE_SIZE = 10 * 1024 * 1024

# Maximum length for search result lines before truncation
_MAX_LINE_LENGTH = 500


def _validate_source_path(file_path: str) -> Path:
    """Validate that a file path is within the project root and resolve it.

    Args:
        file_path: Relative or absolute path to validate.

    Returns:
        Resolved absolute path within the project.

    Raises:
        ValueError: If the path escapes the project root or is invalid.
    """
    project_root = PROJECT_ROOT.resolve()
    resolved = (project_root / file_path).resolve()
    if not resolved.is_relative_to(project_root):
        msg = f"Path traversal denied: {file_path!r} resolves outside project root"
        raise ValueError(msg)
    return resolved


def _is_select_only(sql: str) -> bool:
    """Validate that a SQL string contains only SELECT statements using sqlparse.

    Args:
        sql: The SQL query string to validate.

    Returns:
        True if the query contains only SELECT statements.
    """
    parsed = sqlparse.parse(sql)
    if not parsed:
        return False
    for statement in parsed:
        if not statement.tokens or not str(statement).strip():
            continue
        stmt_type = statement.get_type()
        if stmt_type != "SELECT":
            return False
    return True


async def read_source_file(
    exec_context: ToolExecutionContext,
    file_path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> ToolResult:
    """Read a source file from the project repository.

    Args:
        exec_context: The tool execution context.
        file_path: Path relative to project root.
        start_line: Optional 1-indexed start line.
        end_line: Optional 1-indexed end line (inclusive).

    Returns:
        ToolResult with file contents or error.
    """
    logger.info(
        "read_source_file: path=%s, start=%s, end=%s", file_path, start_line, end_line
    )

    try:
        resolved = _validate_source_path(file_path)
    except ValueError as e:
        return ToolResult(data={"error": str(e)})

    if not resolved.exists():
        return ToolResult(data={"error": f"File not found: {file_path}"})

    if not resolved.is_file():
        return ToolResult(data={"error": f"Not a file: {file_path}"})

    stat = await asyncio.to_thread(resolved.stat)
    if stat.st_size > _MAX_FILE_SIZE:
        return ToolResult(
            data={
                "error": f"File too large ({stat.st_size} bytes, max {_MAX_FILE_SIZE})"
            }
        )

    try:
        async with aiofiles.open(resolved, encoding="utf-8") as f:
            lines = await f.readlines()
    except UnicodeDecodeError:
        return ToolResult(data={"error": f"Cannot read binary file: {file_path}"})
    except OSError as e:
        return ToolResult(data={"error": f"Failed to read file: {e}"})

    total_lines = len(lines)

    if start_line is not None or end_line is not None:
        start_idx = (start_line - 1) if start_line and start_line >= 1 else 0
        end_idx = end_line if end_line and end_line >= 1 else total_lines
        selected = lines[start_idx:end_idx]
        content = "".join(selected)
        return ToolResult(
            data={
                "path": file_path,
                "content": content,
                "start_line": start_idx + 1,
                "end_line": min(end_idx, total_lines),
                "total_lines": total_lines,
            }
        )

    content = "".join(lines)
    return ToolResult(
        data={
            "path": file_path,
            "content": content,
            "total_lines": total_lines,
        }
    )


async def search_source_code(
    exec_context: ToolExecutionContext,
    pattern: str,
    path: str | None = None,
) -> ToolResult:
    """Search the project source code using ripgrep.

    Args:
        exec_context: The tool execution context.
        pattern: Search pattern (regex supported).
        path: Optional subdirectory to restrict search (relative to project root).

    Returns:
        ToolResult with search results or error.
    """
    logger.info("search_source_code: pattern=%s, path=%s", pattern, path)

    project_root = PROJECT_ROOT.resolve()
    search_path = project_root

    if path:
        try:
            search_path = _validate_source_path(path)
        except ValueError as e:
            return ToolResult(data={"error": str(e)})
        if not search_path.exists():
            return ToolResult(data={"error": f"Path not found: {path}"})

    try:
        process = await asyncio.create_subprocess_exec(
            "rg",
            "--max-count=100",
            "--line-number",
            "--no-heading",
            "--color=never",
            "--max-filesize=1M",
            "--",
            pattern,
            str(search_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
    except FileNotFoundError:
        return ToolResult(data={"error": "ripgrep (rg) is not installed"})

    if process.returncode == 1:
        return ToolResult(data={"pattern": pattern, "matches": [], "match_count": 0})

    if process.returncode not in {0, 1}:
        error_msg = stderr.decode("utf-8", errors="replace").strip()
        return ToolResult(data={"error": f"Search failed: {error_msg}"})

    output = stdout.decode("utf-8", errors="replace")
    prefix = str(project_root) + "/"
    all_lines = output.splitlines()
    max_total_matches = 250
    matches: list[str] = []
    for raw_line in all_lines[:max_total_matches]:
        display_line = (
            raw_line[:_MAX_LINE_LENGTH] + "..."
            if len(raw_line) > _MAX_LINE_LENGTH
            else raw_line
        )
        relative_line = display_line.replace(prefix, "", 1)
        matches.append(relative_line)

    return ToolResult(
        data={
            "pattern": pattern,
            "matches": matches,
            "match_count": len(all_lines),
            "truncated": len(all_lines) > max_total_matches,
        }
    )


async def query_database(
    exec_context: ToolExecutionContext,
    query: str,
) -> ToolResult:
    """Execute a read-only SQL query against the application database.

    Uses sqlparse for SELECT-only validation and SET TRANSACTION READ ONLY
    for PostgreSQL defense-in-depth.

    Args:
        exec_context: The tool execution context.
        query: SQL SELECT query to execute.

    Returns:
        ToolResult with query results or error.
    """
    logger.info("query_database: query=%s", query[:200])

    if not _is_select_only(query):
        return ToolResult(data={"error": "Only SELECT queries are allowed"})

    engine = exec_context.db_context.engine

    try:
        # Use a separate connection to avoid interfering with the active
        # transaction in db_context (which may have already executed queries,
        # making SET TRANSACTION READ ONLY fail on PostgreSQL).
        async with engine.begin() as conn:
            dialect = engine.dialect.name
            if dialect == "postgresql":
                await conn.execute(text("SET TRANSACTION READ ONLY"))

            result = await conn.execute(text(query))
            rows = [
                dict(row) for row in result.mappings().fetchmany(_MAX_QUERY_ROWS + 1)
            ]

        if len(rows) > _MAX_QUERY_ROWS:
            rows = rows[:_MAX_QUERY_ROWS]
            return ToolResult(
                data={
                    "rows": rows,
                    "row_count": len(rows),
                    "truncated": True,
                    "max_rows": _MAX_QUERY_ROWS,
                }
            )

        return ToolResult(
            data={
                "rows": rows,
                "row_count": len(rows),
                "truncated": False,
            }
        )
    except Exception as e:
        logger.error("query_database failed: %s", e, exc_info=True)
        return ToolResult(data={"error": f"Query failed: {e}"})


async def read_error_logs(
    exec_context: ToolExecutionContext,
    level: str | None = None,
    logger_name: str | None = None,
    limit: int = 50,
) -> ToolResult:
    """Read application error logs from the database.

    Args:
        exec_context: The tool execution context.
        level: Optional filter by log level (e.g. 'ERROR', 'WARNING').
        logger_name: Optional filter by logger name.
        limit: Maximum number of logs to return (default 50, max 200).

    Returns:
        ToolResult with error log entries.
    """
    logger.info(
        "read_error_logs: level=%s, logger=%s, limit=%d", level, logger_name, limit
    )

    limit = max(1, min(limit, 200))

    db_context = exec_context.db_context
    logs = await db_context.error_logs.get_all(
        level=level,
        logger_name=logger_name,
        limit=limit,
    )

    return ToolResult(
        data={
            "logs": logs,
            "count": len(logs),
            "filters": {
                "level": level,
                "logger_name": logger_name,
                "limit": limit,
            },
        }
    )


async def create_github_issue(
    exec_context: ToolExecutionContext,
    title: str,
    body: str,
) -> ToolResult:
    """Create a GitHub issue in the project repository.

    This tool requires confirmation before execution.
    Requires the GITHUB_TOKEN environment variable to be set.

    Args:
        exec_context: The tool execution context.
        title: Issue title.
        body: Issue body (Markdown supported).

    Returns:
        ToolResult with the created issue URL and number.
    """
    logger.info("create_github_issue: title=%s", title)

    github_token = os.environ.get("GITHUB_TOKEN")
    if not github_token:
        return ToolResult(
            data={"error": "GITHUB_TOKEN environment variable is not set"}
        )

    repo = os.environ.get("GITHUB_REPOSITORY", "werdnum/family-assistant")

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"https://api.github.com/repos/{repo}/issues",
                headers={
                    "Authorization": f"Bearer {github_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json={"title": title, "body": body},
                timeout=30,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            return ToolResult(
                data={
                    "error": f"GitHub API error: {e.response.status_code} {e.response.text}"
                }
            )
        except httpx.RequestError as e:
            return ToolResult(data={"error": f"GitHub API request failed: {e}"})

    issue_data = response.json()
    return ToolResult(
        data={
            "issue_number": issue_data["number"],
            "url": issue_data["html_url"],
            "title": title,
        }
    )


# Tool Definitions

ENGINEERING_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "read_source_file",
            "description": (
                "Read a source file from the project repository. "
                "Useful for examining application code, configuration files, and scripts. "
                "Paths are relative to the project root. "
                "Returns the file content with optional line range selection."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path relative to project root (e.g. 'src/family_assistant/tools/notes.py').",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "Optional 1-indexed start line number.",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Optional 1-indexed end line number (inclusive).",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_source_code",
            "description": (
                "Search the project source code for a pattern using ripgrep. "
                "Supports regex patterns. Returns matching lines with file paths and line numbers. "
                "Optionally restrict search to a subdirectory."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Search pattern (regex supported).",
                    },
                    "path": {
                        "type": "string",
                        "description": "Optional subdirectory to restrict search (relative to project root).",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_database",
            "description": (
                "Execute a read-only SQL SELECT query against the application database. "
                "Only SELECT queries are permitted; all other statement types are rejected. "
                "Results are limited to 1000 rows. Use this to examine application state, "
                "investigate data issues, or gather diagnostic information."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "SQL SELECT query to execute.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_error_logs",
            "description": (
                "Read application error logs from the database. "
                "Useful for diagnosing application errors and warnings. "
                "Can filter by log level and logger name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "level": {
                        "type": "string",
                        "description": "Filter by log level (e.g. 'ERROR', 'WARNING').",
                    },
                    "logger_name": {
                        "type": "string",
                        "description": "Filter by logger name.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of logs to return (default 50, max 200).",
                        "default": 50,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_github_issue",
            "description": (
                "Create a GitHub issue in the project repository to report bugs or "
                "request improvements discovered during debugging. "
                "Requires GITHUB_TOKEN environment variable. "
                "This tool requires user confirmation before execution."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Issue title.",
                    },
                    "body": {
                        "type": "string",
                        "description": "Issue body in Markdown format.",
                    },
                },
                "required": ["title", "body"],
            },
        },
    },
]
