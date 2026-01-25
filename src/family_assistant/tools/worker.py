"""Worker tools for spawning and managing AI worker tasks.

This module provides tools for spawning isolated AI coding agents,
reading task results, and managing worker tasks.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiofiles
import aiofiles.os

from family_assistant.services.worker_backend import get_worker_backend
from family_assistant.tools.types import ToolResult

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


# Tool Definitions
# ast-grep-ignore: no-dict-any - Tool definitions follow OpenAI schema format
WORKER_TOOLS_DEFINITION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "spawn_worker",
            "description": (
                "Spawn an isolated AI coding agent to handle a complex task. "
                "The worker runs in a sandboxed environment with access to the shared workspace "
                "and can use Claude Code or Gemini CLI to complete coding tasks.\n\n"
                "Use this for:\n"
                "- Complex coding tasks requiring file manipulation\n"
                "- Data processing or analysis tasks\n"
                "- Tasks that need general-purpose computing\n"
                "- Long-running operations that shouldn't block the conversation\n\n"
                "The worker will complete the task asynchronously and notify you when done. "
                "Use read_task_result to get the output once notified."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_description": {
                        "type": "string",
                        "description": (
                            "Detailed description of the task for the worker to complete. "
                            "Be specific about what you want done, what files to work with, "
                            "and what output is expected."
                        ),
                    },
                    "model": {
                        "type": "string",
                        "enum": ["claude", "gemini"],
                        "description": "AI model to use for the task (default: claude)",
                        "default": "claude",
                    },
                    "context_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "List of workspace paths to include as context for the worker "
                            "(e.g., ['shared/data/input.csv', 'shared/scripts/'])"
                        ),
                    },
                    "timeout_minutes": {
                        "type": "integer",
                        "description": "Maximum time for task execution in minutes (default: 30)",
                        "default": 30,
                    },
                },
                "required": ["task_description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_task_result",
            "description": (
                "Read the result of a completed worker task. "
                "Use this after receiving notification that a task has completed.\n\n"
                "Returns the task status, output summary, any output files, and error messages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The task ID to read results for",
                    },
                    "include_file_contents": {
                        "type": "boolean",
                        "description": (
                            "Whether to include the contents of output files "
                            "(default: false, just return file paths)"
                        ),
                        "default": False,
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_worker_tasks",
            "description": (
                "List worker tasks for this conversation. "
                "Shows task IDs, status, and basic info."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": [
                            "pending",
                            "submitted",
                            "running",
                            "success",
                            "failed",
                            "timeout",
                            "cancelled",
                        ],
                        "description": "Filter by status (optional)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of tasks to return (default: 10)",
                        "default": 10,
                    },
                },
                "required": [],
            },
        },
    },
]


def _get_workspace_root(exec_context: ToolExecutionContext) -> Path:
    """Get the workspace root path from configuration."""
    if exec_context.processing_service is None:
        raise ValueError("processing_service not available in exec_context")

    app_config = exec_context.processing_service.app_config
    return Path(app_config.ai_worker_config.workspace_mount_path)


def _validate_workspace_path(relative_path: str, workspace_root: Path) -> Path:
    """Validate and resolve a workspace-relative path."""
    normalized = Path(relative_path)
    if normalized.is_absolute():
        raise ValueError(f"Path must be relative, not absolute: {relative_path}")

    full_path = (workspace_root / normalized).resolve()

    try:
        full_path.relative_to(workspace_root.resolve())
    except ValueError as e:
        raise ValueError(f"Path escapes workspace directory: {relative_path}") from e

    return full_path


async def spawn_worker_tool(
    exec_context: ToolExecutionContext,
    task_description: str,
    model: str = "claude",
    context_paths: list[str] | None = None,
    timeout_minutes: int = 30,
) -> ToolResult:
    """Spawn an AI worker task.

    Args:
        exec_context: The tool execution context
        task_description: Description of the task for the worker
        model: AI model to use (claude or gemini)
        context_paths: Optional paths to include as context
        timeout_minutes: Maximum execution time in minutes

    Returns:
        ToolResult with task_id and status
    """
    if exec_context.processing_service is None:
        return ToolResult(data={"error": "Worker feature not available"})

    app_config = exec_context.processing_service.app_config
    worker_config = app_config.ai_worker_config

    if not worker_config.enabled:
        return ToolResult(data={"error": "AI Worker feature is disabled"})

    # Validate timeout
    if timeout_minutes > worker_config.max_timeout_minutes:
        return ToolResult(
            data={
                "error": f"Timeout exceeds maximum of {worker_config.max_timeout_minutes} minutes"
            }
        )

    # Check concurrency limit
    db_context = exec_context.db_context
    running_count = await db_context.worker_tasks.get_running_tasks_count()
    if running_count >= worker_config.max_concurrent_workers:
        return ToolResult(
            data={
                "error": f"Maximum concurrent workers ({worker_config.max_concurrent_workers}) reached. "
                "Please wait for a task to complete."
            }
        )

    # Validate model
    if model not in {"claude", "gemini"}:
        return ToolResult(
            data={"error": f"Invalid model: {model}. Must be 'claude' or 'gemini'"}
        )

    # Generate task ID
    task_id = f"worker-{uuid.uuid4().hex[:12]}"

    # Set up workspace paths
    workspace_root = _get_workspace_root(exec_context)
    task_dir = workspace_root / "tasks" / task_id
    prompt_path = task_dir / "prompt.md"
    output_dir = task_dir / "output"

    try:
        # Create task directory
        await aiofiles.os.makedirs(task_dir, exist_ok=True)
        await aiofiles.os.makedirs(output_dir, exist_ok=True)

        # Write prompt file
        async with aiofiles.open(prompt_path, "w") as f:
            await f.write(task_description)

        # Validate context paths and track any that were skipped
        validated_context_paths: list[str] = []
        skipped_context_paths: list[dict[str, str]] = []
        if context_paths:
            for path in context_paths:
                try:
                    validated = _validate_workspace_path(path, workspace_root)
                    if await aiofiles.os.path.exists(validated):
                        validated_context_paths.append(path)
                    else:
                        skipped_context_paths.append({
                            "path": path,
                            "reason": "does not exist",
                        })
                        logger.warning(f"Context path does not exist: {path}")
                except ValueError as e:
                    skipped_context_paths.append({"path": path, "reason": str(e)})
                    logger.warning(f"Invalid context path {path}: {e}")

        # Build webhook URL
        server_url = app_config.server_url.rstrip("/")
        webhook_url = f"{server_url}/webhook/event"

        # Create database record
        await db_context.worker_tasks.create_task(
            task_id=task_id,
            conversation_id=exec_context.conversation_id,
            interface_type=exec_context.interface_type,
            task_description=task_description,
            model=model,
            context_files=validated_context_paths,
            timeout_minutes=timeout_minutes,
            user_name=exec_context.user_name,
        )

        # Create event listener for completion notification
        await db_context.events.create_event_listener(
            name=f"worker-{task_id}-completion",
            source_id="webhook",
            match_conditions={
                "event_type": "worker_completion",
                "data.task_id": task_id,
            },
            conversation_id=exec_context.conversation_id,
            interface_type=exec_context.interface_type,
            description=f"Notification when worker task {task_id} completes",
            action_type="wake_llm",
            action_config={
                "context": (
                    f"Worker task {task_id} has completed. "
                    f"Use read_task_result('{task_id}') to see the results."
                ),
            },
            one_time=True,
            enabled=True,
        )

        # Get backend and spawn task
        backend = get_worker_backend(worker_config.backend_type)
        try:
            job_id = await backend.spawn_task(
                task_id=task_id,
                prompt_path=str(prompt_path.relative_to(workspace_root)),
                output_dir=str(output_dir.relative_to(workspace_root)),
                webhook_url=webhook_url,
                model=model,
                timeout_minutes=timeout_minutes,
                context_paths=validated_context_paths,
            )
        except Exception as spawn_error:
            # Clean up orphaned database records
            logger.error(f"Backend spawn failed for task {task_id}: {spawn_error}")
            try:
                await db_context.worker_tasks.update_task_status(
                    task_id=task_id,
                    status="failed",
                    error_message=f"Failed to spawn backend: {spawn_error!s}",
                )
            except Exception as cleanup_error:
                logger.error(
                    f"Failed to update task status during cleanup: {cleanup_error}"
                )
            raise

        # Update task status to submitted (started_at set later when task runs)
        await db_context.worker_tasks.update_task_status(
            task_id=task_id,
            status="submitted",
            job_name=job_id,
        )

        logger.info(f"Spawned worker task {task_id} with job {job_id}")

        # Build result with context path warnings if any were skipped
        # ast-grep-ignore: no-dict-any - Tool result data is dynamic
        result_data: dict[str, Any] = {
            "task_id": task_id,
            "status": "submitted",
            "model": model,
            "timeout_minutes": timeout_minutes,
            "message": (
                f"Worker task '{task_id}' has been submitted. "
                "You will be notified when it completes."
            ),
        }

        if skipped_context_paths:
            result_data["skipped_context_paths"] = skipped_context_paths
            result_data["warning"] = (
                f"{len(skipped_context_paths)} context path(s) were skipped due to errors. "
                "See skipped_context_paths for details."
            )

        return ToolResult(data=result_data)

    except Exception as e:
        logger.error(f"Failed to spawn worker task: {e}", exc_info=True)
        return ToolResult(data={"error": f"Failed to spawn worker: {e!s}"})


async def read_task_result_tool(
    exec_context: ToolExecutionContext,
    task_id: str,
    include_file_contents: bool = False,
) -> ToolResult:
    """Read the result of a completed worker task.

    Args:
        exec_context: The tool execution context
        task_id: The task ID to read results for
        include_file_contents: Whether to include output file contents

    Returns:
        ToolResult with task status and output
    """
    db_context = exec_context.db_context

    # Get task from database
    task = await db_context.worker_tasks.get_task(task_id)
    if not task:
        return ToolResult(data={"error": f"Task not found: {task_id}"})

    # Verify conversation access
    if task["conversation_id"] != exec_context.conversation_id:
        return ToolResult(
            data={"error": "Access denied: Task belongs to another conversation"}
        )

    # ast-grep-ignore: no-dict-any - Dynamic result dict for ToolResult.data
    result: dict[str, Any] = {
        "task_id": task_id,
        "status": task["status"],
        "model": task.get("model"),
        "created_at": task.get("created_at"),
        "started_at": task.get("started_at"),
        "completed_at": task.get("completed_at"),
        "duration_seconds": task.get("duration_seconds"),
    }

    if task.get("summary"):
        result["summary"] = task["summary"]

    if task.get("error_message"):
        result["error_message"] = task["error_message"]

    if task.get("exit_code") is not None:
        result["exit_code"] = task["exit_code"]

    # Include output files
    output_files = task.get("output_files") or []
    if output_files:
        result["output_files"] = output_files

        # Optionally include file contents
        if include_file_contents:
            workspace_root = _get_workspace_root(exec_context)
            file_contents: dict[str, str | dict[str, str]] = {}

            for file_info in output_files:
                if isinstance(file_info, dict):
                    file_path = file_info.get("path", "")
                else:
                    file_path = str(file_info)

                if file_path:
                    try:
                        full_path = _validate_workspace_path(file_path, workspace_root)
                        if await aiofiles.os.path.exists(full_path):
                            async with aiofiles.open(full_path) as f:
                                content = await f.read()
                            file_contents[file_path] = content
                        else:
                            file_contents[file_path] = {"error": "File not found"}
                    except (ValueError, OSError) as e:
                        file_contents[file_path] = {"error": str(e)}

            if file_contents:
                result["file_contents"] = file_contents

    return ToolResult(data=result)


async def list_worker_tasks_tool(
    exec_context: ToolExecutionContext,
    status: str | None = None,
    limit: int = 10,
) -> ToolResult:
    """List worker tasks for this conversation.

    Args:
        exec_context: The tool execution context
        status: Optional status filter
        limit: Maximum number of tasks to return

    Returns:
        ToolResult with list of tasks
    """
    db_context = exec_context.db_context

    tasks = await db_context.worker_tasks.get_tasks_for_conversation(
        conversation_id=exec_context.conversation_id,
        status=status,
        limit=limit,
    )

    # Format tasks for display
    task_list = []
    for task in tasks:
        # ast-grep-ignore: no-dict-any - Dynamic result dict for ToolResult.data
        task_info: dict[str, Any] = {
            "task_id": task["task_id"],
            "status": task["status"],
            "model": task.get("model"),
            "created_at": task.get("created_at"),
        }

        if task.get("summary"):
            task_info["summary"] = task["summary"][:100] + (
                "..." if len(task.get("summary", "")) > 100 else ""
            )

        if task.get("error_message"):
            task_info["error"] = task["error_message"][:100] + (
                "..." if len(task.get("error_message", "")) > 100 else ""
            )

        task_list.append(task_info)

    return ToolResult(
        data={
            "tasks": task_list,
            "count": len(task_list),
            "conversation_id": exec_context.conversation_id,
        }
    )
