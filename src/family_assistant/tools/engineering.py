"""Engineering tools for debugging and diagnosing the application.

Provides read-only access to source code, database queries, error logs,
resolved configuration, profile configuration, and live MCP server status,
plus a confirmation-gated MCP reconnect action and GitHub issue creation
for the engineer processing profile.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import os
import platform
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import aiofiles
import httpx
import sqlparse
from sqlalchemy import text

from family_assistant.config_inspection import (
    dump_profile_like,
    redact_sensitive_config,
)
from family_assistant.llm.request_buffer import get_request_buffer
from family_assistant.paths import PROJECT_ROOT
from family_assistant.tool_inventory import (
    TOKEN_ESTIMATE_NOTE,
    inventory_dict_for_service,
)
from family_assistant.tools.infrastructure import (
    PolicyEnforcingToolsProvider,
    ToolDescriptorProvider,
    find_provider_by_type,
)
from family_assistant.tools.mcp import MCPToolsProvider
from family_assistant.tools.types import ToolDefinition, ToolResult
from family_assistant.web.frontend_telemetry import get_frontend_telemetry_buffer

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from family_assistant.config_models import AppConfig
    from family_assistant.storage.database import DatabaseTransaction
    from family_assistant.tools.policy import PolicyEvaluation, ResolvedPolicyRule
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

    # Goes through the transaction API rather than engine.begin(): on SQLite
    # every scope shares one connection, so a raw block would bypass the
    # engine lock, read another unit of work's uncommitted rows, and commit
    # them on exit.
    # ast-grep-ignore: no-dict-any - diagnostic rows have whatever columns the query selected
    async def _run(txn: DatabaseTransaction) -> list[dict[str, object]]:
        if txn.dialect_name == "postgresql":
            await txn.connection.execute(text("SET TRANSACTION READ ONLY"))
        result = await txn.connection.execute(text(query))
        # Bounded fetch: a diagnostic query must not materialize a whole table.
        return [dict(row) for row in result.mappings().fetchmany(_MAX_QUERY_ROWS + 1)]

    try:
        rows = await exec_context.db_context.atomic(_run)

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
        logger.exception("query_database failed: %s", e)
        return ToolResult(data={"error": f"Query failed: {e}"})


async def read_error_logs(
    exec_context: ToolExecutionContext,
    level: str | None = None,
    logger_name: str | None = None,
    limit: int = 50,
    since_hours: int = 168,
    include_extra_data: bool = False,
) -> ToolResult:
    """Read application error logs from the database.

    Args:
        exec_context: The tool execution context.
        level: Optional filter by log level (e.g. 'ERROR', 'WARNING').
        logger_name: Optional filter by logger name.
        limit: Maximum number of logs to return (default 50, max 200).
        since_hours: Recency window - only return logs from the last N hours.
            Always bounded: defaults to 168 (7 days), capped at 720 (30 days,
            the error-log retention default), and must be a positive integer.
            The bound is global rather than per-profile because the policy
            matcher cannot fire on an omitted argument, so an unbounded default
            could not be denied for confined profiles.
        include_extra_data: When False (the default), the freeform ``extra_data``
            field is stripped from the results. Unlike the message and traceback
            (developer-authored text and code-structure), ``extra_data`` is
            arbitrary JSON attached at log time (request bodies, tokens, whole
            objects), so it is the field most likely to carry sensitive or bulky
            content. Human-supervised contexts (e.g. the engineer profile) pass
            True to see it. The default is global rather than per-profile because
            the policy matcher cannot enforce argument defaults (a deny rule on a
            truthy value never fires when the argument is simply omitted).

    Returns:
        ToolResult with error log entries. Tracebacks are always included (they are
        call stack / source structure, not data); only ``extra_data`` is gated.
    """
    # Script kwargs bypass schema coercion, and the policy matcher compares
    # exact types, so a truthy non-bool like "true" would slip past a deny rule
    # on `include_extra_data: true` while still enabling the field below.
    # Reject anything that is not a real bool.
    if not isinstance(include_extra_data, bool):
        error_msg = (
            "include_extra_data must be a boolean (true/false), got "
            f"{type(include_extra_data).__name__}: {include_extra_data!r}"
        )
        return ToolResult(text=f"Error: {error_msg}", data={"error": error_msg})

    # Same strict validation as include_extra_data: script kwargs bypass schema
    # coercion, so a non-int (or a non-positive value that used to mean
    # "unbounded") must be rejected rather than silently widening the window.
    if isinstance(since_hours, bool) or not isinstance(since_hours, int):
        error_msg = (
            "since_hours must be a positive integer number of hours, got "
            f"{type(since_hours).__name__}: {since_hours!r}"
        )
        return ToolResult(text=f"Error: {error_msg}", data={"error": error_msg})
    if since_hours <= 0:
        error_msg = f"since_hours must be positive, got {since_hours}"
        return ToolResult(text=f"Error: {error_msg}", data={"error": error_msg})
    since_hours = min(since_hours, 720)

    logger.info(
        "read_error_logs: level=%s, logger=%s, limit=%d, since_hours=%s, extra_data=%s",
        level,
        logger_name,
        limit,
        since_hours,
        include_extra_data,
    )

    limit = max(1, min(limit, 200))

    now = exec_context.clock.now() if exec_context.clock else datetime.now(UTC)
    since = now - timedelta(hours=since_hours)

    db_context = exec_context.db_context
    logs = await db_context.error_logs.get_all(
        level=level,
        logger_name=logger_name,
        since=since,
        limit=limit,
    )

    if not include_extra_data:
        logs = [{k: v for k, v in log.items() if k != "extra_data"} for log in logs]  # type: ignore[misc]  # sanitized rows drop the optional extra_data key

    return ToolResult(
        data={
            "logs": logs,
            "count": len(logs),
            "filters": {
                "level": level,
                "logger_name": logger_name,
                "limit": limit,
                "since_hours": since_hours,
                "include_extra_data": include_extra_data,
            },
        }
    )


async def get_llm_request_history(
    limit: int = 5,
    minutes: int | None = None,
) -> ToolResult:
    """Get recent LLM request/response history from the in-memory ring buffer.

    Args:
        limit: Maximum number of records to return (default 5, max 100).
        minutes: Optional filter to only include records from the last N minutes.

    Returns:
        ToolResult with LLM request history records.
    """
    logger.info("get_llm_request_history: limit=%d, minutes=%s", limit, minutes)

    limit = max(1, min(limit, 100))

    buffer = get_request_buffer()
    records = buffer.get_recent(limit=limit, since_minutes=minutes)

    return ToolResult(
        data={
            "records": [r.to_dict() for r in records],
            "count": len(records),
            "filters": {
                "limit": limit,
                "minutes": minutes,
            },
        }
    )


async def read_frontend_telemetry(
    limit: int = 50,
    minutes: int | None = None,
    component: str | None = None,
) -> ToolResult:
    """Read recent non-error frontend telemetry (breadcrumbs) from the ring buffer.

    Frontend clients (notably the iOS app) emit diagnostic breadcrumbs — stream
    restarts/disconnects, resync phases, per-operation transport events — that are
    telemetry, not errors, so they are kept out of ``read_error_logs`` and held in a
    separate in-memory ring buffer. Use this to investigate intermittent connection
    or sync problems; use ``read_error_logs`` for genuine errors.

    Args:
        limit: Maximum number of records to return (default 50, max 500).
        minutes: Optional filter to only include records from the last N minutes.
        component: Optional exact-match filter on the reporting component
            (e.g. "Chat.streamDisconnect", "Chat.resync").

    Returns:
        ToolResult with telemetry records, newest first.
    """
    logger.info(
        "read_frontend_telemetry: limit=%d, minutes=%s, component=%s",
        limit,
        minutes,
        component,
    )

    limit = max(1, min(limit, 500))

    buffer = get_frontend_telemetry_buffer()
    records = buffer.get_recent(limit=limit, since_minutes=minutes, component=component)

    return ToolResult(
        data={
            "records": [r.to_dict() for r in records],
            "count": len(records),
            "filters": {
                "limit": limit,
                "minutes": minutes,
                "component": component,
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


def _resolve_mcp_provider(
    exec_context: ToolExecutionContext,
) -> MCPToolsProvider | None:
    """Locate the live MCPToolsProvider in the runtime tools provider tree.

    Returns ``None`` when no tools provider is wired into the execution
    context, or when no MCPToolsProvider was configured. Callers should
    surface a clear diagnostic in that case rather than treating it as a
    connection failure.
    """
    tools_provider = exec_context.tools_provider
    if (
        tools_provider is None
        and exec_context.processing_service is not None
        and hasattr(exec_context.processing_service, "tools_provider")
    ):
        tools_provider = exec_context.processing_service.tools_provider

    if tools_provider is None:
        return None

    return find_provider_by_type(tools_provider, MCPToolsProvider)


async def get_mcp_server_status(
    exec_context: ToolExecutionContext,
) -> ToolResult:
    """Return the live connection status of all configured MCP servers.

    Includes per-server status (``connected`` / ``failed`` / ``cancelled`` /
    ``pending`` / ``connecting``), transport, the discovered tool list, and
    whether a session is currently active. Tokens are omitted; URLs and stdio
    commands are included verbatim so the engineer can correlate against the
    deployment config.

    ``reconnect_attempts`` and ``next_reconnect_in_seconds`` describe the
    retry backoff: a server that has been down for a while is retried less
    and less often (up to half an hour between attempts), so a failed server
    with a long window ahead of it is being paced, not ignored.
    ``reconnect_mcp_server`` bypasses that window.
    """
    logger.info("get_mcp_server_status: dumping live MCP server statuses")

    mcp_provider = _resolve_mcp_provider(exec_context)
    if mcp_provider is None:
        return ToolResult(
            data={
                "error": (
                    "No MCPToolsProvider available in the current tools provider tree. "
                    "Either no MCP servers are configured, "
                    "or the tools provider has not been wired into this execution context."
                ),
                "servers": {},
            }
        )

    statuses = mcp_provider.get_server_statuses()
    summary = {
        "total": len(statuses),
        "connected": sum(
            1 for entry in statuses.values() if entry["status"] == "connected"
        ),
        "failed": sum(1 for entry in statuses.values() if entry["status"] == "failed"),
        "cancelled": sum(
            1 for entry in statuses.values() if entry["status"] == "cancelled"
        ),
        "pending": sum(
            1
            for entry in statuses.values()
            if entry["status"] in {"pending", "connecting"}
        ),
    }
    return ToolResult(data={"summary": summary, "servers": statuses})


async def reconnect_mcp_server(
    exec_context: ToolExecutionContext,
    server_id: str,
) -> ToolResult:
    """Tear down and re-establish an MCP server's session.

    This is a state-changing diagnostic action: it closes any existing
    session for ``server_id``, runs the regular discovery flow, and updates
    the in-memory tool registry. The engineer profile gates this tool with a
    confirmation policy.
    """
    logger.info("reconnect_mcp_server: server_id=%s", server_id)

    mcp_provider = _resolve_mcp_provider(exec_context)
    if mcp_provider is None:
        return ToolResult(
            data={
                "error": (
                    "No MCPToolsProvider available; cannot reconnect. "
                    "Check that the tools provider is wired into this execution context."
                ),
                "server_id": server_id,
            }
        )

    try:
        success = await mcp_provider.reconnect_server(server_id)
    except KeyError:
        return ToolResult(
            data={
                "error": f"Unknown MCP server id: {server_id!r}",
                "configured_servers": sorted(mcp_provider.server_configs.keys()),
            }
        )

    statuses = mcp_provider.get_server_statuses()
    return ToolResult(
        data={
            "server_id": server_id,
            "success": success,
            "status": statuses.get(server_id, {}),
        }
    )


def _resolve_app_config(
    exec_context: ToolExecutionContext,
) -> AppConfig | None:
    """Return the ``AppConfig`` instance from the execution context, if any.

    Engineer-profile diagnostic tools rely on ``processing_service.app_config``
    being available. ``None`` indicates the context is missing infrastructure
    and the caller should surface a clear error.
    """
    processing_service = exec_context.processing_service
    if processing_service is None:
        return None
    return getattr(processing_service, "app_config", None)


async def get_resolved_config(
    exec_context: ToolExecutionContext,
) -> ToolResult:
    """Return the live ``AppConfig`` (excluding service profiles) with secrets redacted.

    Service profile and default-profile bodies are omitted because they can
    be large; use ``get_profile_config`` to retrieve them individually. The
    profile id list is included so the caller knows what's available.
    """
    logger.info("get_resolved_config: dumping live application config")

    app_config = _resolve_app_config(exec_context)
    if app_config is None:
        return ToolResult(
            data={
                "error": (
                    "No AppConfig available on the processing service. "
                    "This tool must be invoked from within a live processing flow."
                )
            }
        )

    full_dump = app_config.model_dump(mode="json")
    profiles = full_dump.pop("service_profiles", [])
    full_dump.pop("default_profile_settings", None)

    profile_ids: list[str] = sorted(
        entry["id"]
        for entry in profiles
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    )

    return ToolResult(
        data={
            "config": redact_sensitive_config(full_dump),
            "profile_ids": profile_ids,
            "profile_count": len(profile_ids),
            "default_service_profile_id": full_dump.get("default_service_profile_id"),
        }
    )


async def get_profile_config(
    exec_context: ToolExecutionContext,
    profile_id: str | None = None,
) -> ToolResult:
    """Return live profile configuration with secrets redacted.

    With ``profile_id``, returns just that profile's config. Without one,
    returns the profile id list (call again with a specific id to fetch the
    body) plus the merged ``default_profile_settings``. This avoids dumping
    every profile in one giant payload.
    """
    logger.info("get_profile_config: profile_id=%s", profile_id)

    app_config = _resolve_app_config(exec_context)
    if app_config is None:
        return ToolResult(
            data={
                "error": (
                    "No AppConfig available on the processing service. "
                    "This tool must be invoked from within a live processing flow."
                )
            }
        )

    profiles = list(getattr(app_config, "service_profiles", []) or [])

    if profile_id is None:
        return ToolResult(
            data={
                "profile_ids": sorted(p.id for p in profiles),
                "default_service_profile_id": getattr(
                    app_config, "default_service_profile_id", None
                ),
                "default_profile_settings": redact_sensitive_config(
                    dump_profile_like(app_config.default_profile_settings)
                ),
            }
        )

    matched = next((p for p in profiles if p.id == profile_id), None)
    if matched is None:
        return ToolResult(
            data={
                "error": f"Profile {profile_id!r} not found",
                "profile_ids": sorted(p.id for p in profiles),
            }
        )

    return ToolResult(
        data={
            "id": matched.id,
            "description": matched.description,
            "config": redact_sensitive_config(dump_profile_like(matched)),
        }
    )


def _resolve_services_registry(
    exec_context: ToolExecutionContext,
) -> Mapping[str, object] | None:
    """Return the live profile->service registry from the execution context.

    The registry is the shared mapping every profile's service holds a
    reference to, so any profile (including engineer) can introspect the
    resolved tool set of its siblings. ``None`` means the context is missing
    infrastructure and the caller should surface a clear error.
    """
    processing_service = exec_context.processing_service
    if processing_service is None:
        return None
    registry = getattr(processing_service, "processing_services_registry", None)
    if not registry:
        return None
    return registry


async def get_profile_tool_inventory(
    exec_context: ToolExecutionContext,
    profile_id: str | None = None,
    can_confirm: bool = True,
) -> ToolResult:
    """Return the resolved per-profile tool advertisement for bloat analysis.

    Each profile advertises an ``eager`` set of tools to the LLM on every turn
    plus, when on-demand tools exist, the ``activate_tools`` meta-tool. The
    ``eager`` token estimate is the per-turn cost (the main driver of tool
    bloat); ``on_demand`` tools are hidden behind progressive disclosure until
    activated and cost nothing until then. ``by_source`` attributes the surface
    to ``local`` tools vs each ``mcp:<server_id>``.

    Without ``profile_id`` this returns a summary across all live profiles
    (counts and token estimates, no per-tool lists, sorted by per-turn cost).
    With ``profile_id`` it returns the full per-tool breakdown for that profile.

    ``can_confirm`` models the per-turn confirmation capability; pass ``false``
    to see the surface for interactions that cannot prompt for confirmation.
    Token figures are a heuristic (serialized JSON characters / 4) for relative
    comparison, not an exact provider token count.
    """
    logger.info(
        "get_profile_tool_inventory: profile_id=%s can_confirm=%s",
        profile_id,
        can_confirm,
    )

    registry = _resolve_services_registry(exec_context)
    if registry is None:
        return ToolResult(
            data={
                "error": (
                    "No live processing-service registry available. This tool must "
                    "be invoked from within a live processing flow."
                )
            }
        )

    if profile_id is not None and profile_id not in registry:
        return ToolResult(
            data={
                "error": f"Profile {profile_id!r} not found in the live registry",
                "profile_ids": sorted(registry.keys()),
            }
        )

    target_ids = [profile_id] if profile_id is not None else sorted(registry.keys())

    # Summary mode (all profiles) drops per-tool lists for a compact payload;
    # a single requested profile gets the full per-tool breakdown.
    include_tools = profile_id is not None
    inventories = [
        await inventory_dict_for_service(
            pid, registry[pid], can_confirm=can_confirm, include_tools=include_tools
        )
        for pid in target_ids
    ]

    if profile_id is None:
        inventories.sort(
            key=lambda entry: entry.get("advertised_per_turn_tokens", 0),
            reverse=True,
        )

    return ToolResult(
        data={
            "can_confirm": can_confirm,
            "token_estimate_note": TOKEN_ESTIMATE_NOTE,
            "profiles": inventories,
            "profile_count": len(inventories),
        }
    )


def _serialize_policy_rule(resolved_rule: ResolvedPolicyRule) -> dict[str, object]:
    """Serialize a resolved policy rule for diagnostic output."""
    return {
        "layer": resolved_rule.layer,
        "priority": resolved_rule.rule.priority,
        "effective_priority": resolved_rule.effective_priority,
        "decision": str(resolved_rule.decision),
        "description": resolved_rule.description,
        "match": resolved_rule.match.model_dump(mode="json", exclude_none=True),
    }


def _serialize_policy_evaluation(evaluation: PolicyEvaluation) -> dict[str, object]:
    """Serialize a policy evaluation (decision, reason, matched rule)."""
    return {
        "decision": str(evaluation.decision),
        "reason": evaluation.reason,
        "matched_rule": (
            _serialize_policy_rule(evaluation.matched_rule)
            if evaluation.matched_rule is not None
            else None
        ),
    }


async def resolve_tool_policy(
    exec_context: ToolExecutionContext,
    tool_name: str,
    profile_id: str | None = None,
    arguments: dict[str, object] | None = None,
    can_confirm: bool = True,
) -> ToolResult:
    """Resolve the live tool-policy decision for a tool name per profile.

    For each targeted profile, evaluates the tool's descriptor against the
    profile's live policy engine three ways (raw policy, advertisement, and
    execution) and reports which rule matched, from which layer, with what
    priority and description — answering "why can't profile X use tool Y"
    from live state rather than guesswork.

    Args:
        exec_context: The tool execution context.
        tool_name: Name of the tool to resolve policy for.
        profile_id: Optional profile id; omit to evaluate every live profile.
        arguments: Optional hypothetical call arguments for
            argument-conditional (argument_equals) rules.
        can_confirm: Whether the interaction can prompt for confirmation.

    Returns:
        ToolResult with per-profile policy resolutions or error.
    """
    # Script kwargs bypass schema coercion (same pattern as read_error_logs),
    # so reject non-bool / non-dict values instead of silently coercing them.
    if not isinstance(can_confirm, bool):
        error_msg = (
            "can_confirm must be a boolean (true/false), got "
            f"{type(can_confirm).__name__}: {can_confirm!r}"
        )
        return ToolResult(text=f"Error: {error_msg}", data={"error": error_msg})

    if arguments is not None and not isinstance(arguments, dict):
        error_msg = (
            "arguments must be an object mapping argument names to values, got "
            f"{type(arguments).__name__}: {arguments!r}"
        )
        return ToolResult(text=f"Error: {error_msg}", data={"error": error_msg})

    logger.info(
        "resolve_tool_policy: tool_name=%s profile_id=%s can_confirm=%s arguments=%s",
        tool_name,
        profile_id,
        can_confirm,
        arguments,
    )

    registry = _resolve_services_registry(exec_context)
    if registry is None:
        return ToolResult(
            data={
                "error": (
                    "No live processing-service registry available. This tool must "
                    "be invoked from within a live processing flow."
                )
            }
        )

    if profile_id is not None and profile_id not in registry:
        return ToolResult(
            data={
                "error": f"Profile {profile_id!r} not found in the live registry",
                "profile_ids": sorted(registry.keys()),
            }
        )

    target_ids = [profile_id] if profile_id is not None else sorted(registry.keys())

    entries: list[dict[str, object]] = []
    descriptor_found = False
    known_names: set[str] = set()

    for pid in target_ids:
        service = registry[pid]
        tools_provider = getattr(service, "tools_provider", None)
        if tools_provider is None:
            entries.append({
                "profile_id": pid,
                "error": "No tools provider wired into this profile's service.",
            })
            continue

        policy_provider = find_provider_by_type(
            tools_provider, PolicyEnforcingToolsProvider
        )
        if policy_provider is None:
            entries.append({
                "profile_id": pid,
                "error": (
                    "No policy-enforcing provider in this profile's tools "
                    "provider chain."
                ),
            })
            continue

        # The pre-policy root provider: policy-DENIED tools are invisible on
        # the policy provider itself, so the descriptor must come from the
        # wrapped provider to explain a denial.
        pre_policy_provider = policy_provider.wrapped_provider
        if not isinstance(pre_policy_provider, ToolDescriptorProvider):
            entries.append({
                "profile_id": pid,
                "error": (
                    "The policy provider's wrapped provider does not expose "
                    "tool descriptors."
                ),
            })
            continue

        descriptor = await pre_policy_provider.get_tool_descriptor(tool_name)
        if descriptor is None:
            known_names.update(
                d.name for d in await pre_policy_provider.get_tool_descriptors()
            )
            entries.append({
                "profile_id": pid,
                "error": (
                    f"Tool {tool_name!r} is not registered in this profile's "
                    "provider (before policy filtering)."
                ),
            })
            continue

        descriptor_found = True
        engine = policy_provider.policy_engine

        on_demand_view = getattr(service, "on_demand_view", None)
        on_demand = bool(
            on_demand_view is not None
            and (
                descriptor.name in on_demand_view.on_demand_tool_names
                or (
                    descriptor.mcp_server_id is not None
                    and descriptor.mcp_server_id
                    in on_demand_view.on_demand_mcp_server_ids
                )
            )
        )

        entries.append({
            "profile_id": pid,
            "default_decision": str(engine.default_decision),
            "origin": descriptor.origin,
            "mcp_server_id": descriptor.mcp_server_id,
            "tags": sorted(str(tag) for tag in descriptor.tags),
            "on_demand": on_demand,
            "raw": _serialize_policy_evaluation(
                engine.evaluate(descriptor, arguments=arguments)
            ),
            "advertisement": _serialize_policy_evaluation(
                engine.evaluate_for_advertisement(descriptor, can_confirm=can_confirm)
            ),
            "execution": _serialize_policy_evaluation(
                engine.evaluate_for_execution(
                    descriptor, arguments=arguments, can_confirm=can_confirm
                )
            ),
        })

    if not descriptor_found:
        sorted_names = sorted(known_names)
        similar = difflib.get_close_matches(tool_name, sorted_names, n=10, cutoff=0.5)
        substring_matches = [
            name
            for name in sorted_names
            if tool_name.lower() in name.lower() and name not in similar
        ]
        return ToolResult(
            data={
                "tool_name": tool_name,
                "tool_exists": False,
                "similar_tool_names": [*similar, *substring_matches][:10],
                "profiles": entries,
                "profile_count": len(entries),
            }
        )

    data: dict[str, object] = {
        "tool_name": tool_name,
        "tool_exists": True,
        "can_confirm": can_confirm,
        "arguments": arguments,
        "profiles": entries,
        "profile_count": len(entries),
    }
    if arguments is None:
        data["note"] = (
            "No arguments were provided, so rules with argument_equals matchers "
            "cannot match. Pass hypothetical call arguments to test "
            "argument-conditional rules."
        )
    return ToolResult(data=data)


async def get_system_info(
    exec_context: ToolExecutionContext,
) -> ToolResult:
    """Return runtime environment info (Python, platform, database dialect).

    Mirrors the ``system_info`` block of the ``/api/diagnostics/export``
    endpoint so the engineer profile can include it in bug reports without
    leaving the chat.
    """
    logger.info("get_system_info: gathering runtime environment info")

    db_context = exec_context.db_context
    db_dialect = (
        "unknown"
        if db_context is None
        else getattr(db_context, "dialect_name", "unknown")
    )

    return ToolResult(
        data={
            "python_version": sys.version.split()[0],
            "platform": platform.platform(),
            "database_dialect": db_dialect,
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
                "Can filter by log level, logger name, and time window. "
                "Results include the message, exception, and traceback; the freeform "
                "extra_data field is omitted unless include_extra_data is true."
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
                    "since_hours": {
                        "type": "integer",
                        "description": (
                            "Only return logs from the last N hours. Must be a "
                            "positive integer; defaults to 168 (7 days) and is "
                            "capped at 720 (30 days, the log retention limit)."
                        ),
                        "default": 168,
                    },
                    "include_extra_data": {
                        "type": "boolean",
                        "description": (
                            "Include the freeform extra_data field (arbitrary JSON "
                            "logged as context), which may contain sensitive or bulky "
                            "data. Defaults to false. Tracebacks are always included."
                        ),
                        "default": False,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_llm_request_history",
            "description": (
                "Get recent LLM request/response history from the in-memory ring buffer. "
                "Shows what requests were sent to the LLM and what responses were received, "
                "including model ID, messages, tools, and timing. "
                "Useful for debugging LLM behavior, understanding tool calls, and diagnosing issues."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of records to return (default 5, max 100).",
                        "default": 5,
                    },
                    "minutes": {
                        "type": "integer",
                        "description": "Optional filter to only include records from the last N minutes.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_frontend_telemetry",
            "description": (
                "Read recent non-error frontend telemetry (breadcrumbs) from the in-memory "
                "ring buffer. Frontend clients (notably the iOS app) emit diagnostic "
                "breadcrumbs for stream restarts/disconnects, resync phases, and per-operation "
                "transport events. These are telemetry, not errors, so they are kept OUT of "
                "read_error_logs and held here instead. Use this to investigate intermittent "
                "connection or sync problems; use read_error_logs for genuine errors."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of records to return (default 50, max 500).",
                        "default": 50,
                    },
                    "minutes": {
                        "type": "integer",
                        "description": "Optional filter to only include records from the last N minutes.",
                    },
                    "component": {
                        "type": "string",
                        "description": (
                            "Optional exact-match filter on the reporting component "
                            '(e.g. "Chat.streamDisconnect", "Chat.resync").'
                        ),
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
    {
        "type": "function",
        "function": {
            "name": "get_mcp_server_status",
            "description": (
                "Return the live connection status of every configured MCP server, "
                "including transport, command/url, session activity, and the list "
                "of tools each server currently provides. Use this when scripts or "
                "the LLM report that an MCP-backed tool name is undefined, or to "
                "confirm an MCP server is reachable before depending on it."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reconnect_mcp_server",
            "description": (
                "Tear down and re-establish the connection to a single MCP server. "
                "Useful when a server has dropped to a 'failed' or 'cancelled' "
                "state and you want to retry without restarting the application. "
                "This is a state-changing operation; the engineer profile gates "
                "it behind user confirmation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "server_id": {
                        "type": "string",
                        "description": (
                            "Configured MCP server id (matches a key in "
                            "AppConfig.mcp_servers). Use get_mcp_server_status "
                            "to see available ids."
                        ),
                    },
                },
                "required": ["server_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_resolved_config",
            "description": (
                "Return the live AppConfig (with secrets redacted), excluding the "
                "service profile bodies which can be large. Use get_profile_config "
                "to fetch a profile by id. The response includes the full list of "
                "configured profile ids so you know what's available."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_profile_config",
            "description": (
                "Return the live configuration of a service profile (with secrets "
                "redacted). Without profile_id returns the list of available "
                "profile ids plus the merged default_profile_settings; with "
                "profile_id returns just that profile's body, including merged "
                "operator_tools_policy."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "profile_id": {
                        "type": "string",
                        "description": (
                            "Service profile id to dump. Omit to list available "
                            "profile ids and view default_profile_settings."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_profile_tool_inventory",
            "description": (
                "Diagnose per-profile tool bloat: the resolved set of tools each "
                "profile advertises to the LLM, split into 'eager' (sent every "
                "turn — the main token cost) vs 'on_demand' (hidden behind the "
                "activate_tools meta-tool until activated). Each tool has a "
                "serialized size and heuristic token estimate, and by_source "
                "attributes the surface to 'local' tools vs each 'mcp:<server_id>'. "
                "Without profile_id returns a summary across all profiles sorted by "
                "per-turn cost; with profile_id returns the full per-tool breakdown. "
                "Token figures are a heuristic (JSON chars / 4), not exact."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "profile_id": {
                        "type": "string",
                        "description": (
                            "Profile id for a full per-tool breakdown. Omit for a "
                            "summary across all profiles."
                        ),
                    },
                    "can_confirm": {
                        "type": "boolean",
                        "description": (
                            "Model the per-turn confirmation capability. When false, "
                            "confirmation-gated tools the policy would drop are "
                            "excluded. Defaults to true."
                        ),
                        "default": True,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_tool_policy",
            "description": (
                "Resolve the live tool-policy decision (allow/deny/confirm) for "
                "a tool name against a profile's policy engine, explaining which "
                "rule matched (layer, priority, description) and the resolved "
                "default decision. Use it to check whether and why a tool is "
                "available to the engineer or any other profile — e.g. before "
                "reporting a tool as missing, or when a tool call was denied. "
                "Pass arguments to test argument-conditional rules such as "
                "delegate_to_service targets; omit profile_id to compare the "
                "decision across all live profiles."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "description": "Name of the tool to resolve policy for.",
                    },
                    "profile_id": {
                        "type": "string",
                        "description": (
                            "Service profile id whose policy engine to evaluate. "
                            "Omit to evaluate every live profile."
                        ),
                    },
                    "arguments": {
                        "type": "object",
                        "additionalProperties": True,
                        "description": (
                            "Hypothetical call arguments, used to test "
                            "argument-conditional rules (argument_equals "
                            "matchers). Without arguments those rules cannot "
                            "match."
                        ),
                    },
                    "can_confirm": {
                        "type": "boolean",
                        "description": (
                            "Whether the interaction can prompt the user for "
                            "confirmation. When false, confirm-gated tools "
                            "resolve to deny for advertisement/execution."
                        ),
                        "default": True,
                    },
                },
                "required": ["tool_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_info",
            "description": (
                "Return runtime environment info (Python version, OS platform, "
                "database dialect). Mirrors the system_info block of "
                "/api/diagnostics/export so it can be included in bug reports."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]
