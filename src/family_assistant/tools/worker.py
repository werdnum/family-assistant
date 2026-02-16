"""Worker tools for spawning and managing AI worker tasks.

This module provides tools for spawning isolated AI coding agents,
reading task results, and managing worker tasks.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from typing import TYPE_CHECKING, Any

import aiofiles
import aiofiles.os

from family_assistant.services.worker_backend import WorkerStatus, get_worker_backend
from family_assistant.tools.types import ToolResult
from family_assistant.utils.workspace import get_workspace_root, validate_workspace_path

if TYPE_CHECKING:
    from family_assistant.services.worker_backend import WorkerBackend
    from family_assistant.storage.context import DatabaseContext
    from family_assistant.tools.types import ToolDefinition, ToolExecutionContext

logger = logging.getLogger(__name__)


# Tool Definitions
WORKER_TOOLS_DEFINITION: list[ToolDefinition] = [
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
                    "agent": {
                        "type": "string",
                        "enum": ["claude", "gemini"],
                        "description": (
                            "AI coding tool to use. NOT a model checkpoint - "
                            "use only the exact values listed. (default: claude)"
                        ),
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
            "name": "cancel_worker_task",
            "description": (
                "Cancel a running or stuck worker task. "
                "Use this to free up concurrency slots when tasks are stuck or no longer needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The task ID to cancel",
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


_TERMINAL_STATUSES = {
    WorkerStatus.SUCCESS,
    WorkerStatus.FAILED,
    WorkerStatus.TIMEOUT,
    WorkerStatus.CANCELLED,
}

_STATUS_MAP = {
    WorkerStatus.SUCCESS: "success",
    WorkerStatus.FAILED: "failed",
    WorkerStatus.TIMEOUT: "timeout",
    WorkerStatus.CANCELLED: "cancelled",
}

_TERMINAL_DB_STATUSES = set(_STATUS_MAP.values())


async def reconcile_stale_tasks(
    db_context: DatabaseContext, backend: WorkerBackend
) -> int:
    """Check active DB tasks against backend state and mark stale ones as failed.

    For each task with status "submitted" or "running" in the DB:
    - If it has no job_name, mark as failed (spawn never completed)
    - If backend reports a terminal status, update DB accordingly
    - If backend still shows active, leave it alone

    Returns:
        Number of tasks reconciled
    """
    active_tasks = await db_context.worker_tasks.get_active_tasks()
    if not active_tasks:
        return 0

    reconciled = 0
    for task in active_tasks:
        task_id = task["task_id"]
        job_name = task.get("job_name")

        if not job_name:
            await db_context.worker_tasks.update_task_status(
                task_id=task_id,
                status="failed",
                error_message="Task has no job_name — spawn never completed",
            )
            reconciled += 1
            logger.info(f"Reconciled task {task_id}: no job_name, marked failed")
            continue

        try:
            result = await backend.get_task_status(job_name)
        except Exception:
            logger.warning(
                f"Failed to check backend status for task {task_id} (job {job_name})",
                exc_info=True,
            )
            continue

        if result.status in _TERMINAL_STATUSES:
            db_status = _STATUS_MAP.get(result.status, "failed")
            await db_context.worker_tasks.update_task_status(
                task_id=task_id,
                status=db_status,
                error_message=result.error_message
                or f"Reconciled from backend status: {result.status.value}",
                exit_code=result.exit_code,
            )
            reconciled += 1
            logger.info(
                f"Reconciled task {task_id}: backend status {result.status.value} → {db_status}"
            )

    if reconciled:
        logger.info(f"Reconciled {reconciled} stale worker tasks")
    return reconciled


async def cancel_worker_task_tool(
    exec_context: ToolExecutionContext,
    task_id: str,
) -> ToolResult:
    """Cancel a worker task.

    Args:
        exec_context: The tool execution context
        task_id: The task ID to cancel

    Returns:
        ToolResult with cancellation status
    """
    if exec_context.processing_service is None:
        return ToolResult(data={"error": "Worker feature not available"})

    db_context = exec_context.db_context

    task = await db_context.worker_tasks.get_task(task_id)
    if not task:
        return ToolResult(data={"error": f"Task not found: {task_id}"})

    # Verify conversation access
    if task["conversation_id"] != exec_context.conversation_id:
        return ToolResult(
            data={"error": "Access denied: Task belongs to another conversation"}
        )

    if task["status"] in _TERMINAL_DB_STATUSES:
        return ToolResult(
            data={
                "error": f"Task already in terminal state: {task['status']}",
                "task_id": task_id,
                "status": task["status"],
            }
        )

    app_config = exec_context.processing_service.app_config
    worker_config = app_config.ai_worker_config

    # Cancel via backend if we have a job_name
    job_name = task.get("job_name")
    if job_name:
        backend = get_worker_backend(
            worker_config.backend_type,
            workspace_root=worker_config.workspace_mount_path,
            docker_config=worker_config.docker,
            kubernetes_config=worker_config.kubernetes,
        )
        try:
            await backend.cancel_task(job_name)
        except Exception as e:
            logger.warning(
                f"Backend cancel failed for task {task_id} (job {job_name}): {e}"
            )

    # Update DB status to cancelled regardless of backend result
    await db_context.worker_tasks.update_task_status(
        task_id=task_id,
        status="cancelled",
        error_message="Cancelled by user",
    )

    logger.info(f"Cancelled worker task {task_id}")
    return ToolResult(
        data={
            "task_id": task_id,
            "status": "cancelled",
            "message": f"Worker task '{task_id}' has been cancelled.",
        }
    )


async def spawn_worker_tool(
    exec_context: ToolExecutionContext,
    task_description: str,
    agent: str = "claude",
    context_paths: list[str] | None = None,
    timeout_minutes: int = 30,
) -> ToolResult:
    """Spawn an AI worker task.

    Args:
        exec_context: The tool execution context
        task_description: Description of the task for the worker
        agent: AI coding tool to use (e.g. claude or gemini)
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

    # Validate agent
    if agent not in set(worker_config.available_agents):
        return ToolResult(
            data={
                "error": f"Invalid agent: {agent}. Must be one of: {worker_config.available_agents}"
            }
        )

    # Generate task ID with full UUID for 128-bit entropy
    task_id = uuid.uuid4().hex

    # Set up workspace paths
    workspace_root = get_workspace_root(exec_context)
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
                    validated = validate_workspace_path(path, workspace_root)
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

        # Build webhook URL (use configured URL or fall back to server_url)
        if worker_config.webhook_url:
            webhook_url = worker_config.webhook_url.rstrip("/")
        else:
            server_url = app_config.server_url.rstrip("/")
            webhook_url = f"{server_url}/webhook/event"

        # Generate callback token for webhook verification (32 bytes = 64 hex chars)
        callback_token = secrets.token_hex(32)

        # Create database record
        await db_context.worker_tasks.create_task(
            task_id=task_id,
            conversation_id=exec_context.conversation_id,
            interface_type=exec_context.interface_type,
            task_description=task_description,
            model=agent,
            context_files=validated_context_paths,
            timeout_minutes=timeout_minutes,
            user_name=exec_context.user_name,
            callback_token=callback_token,
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
        backend = get_worker_backend(
            worker_config.backend_type,
            workspace_root=str(workspace_root),
            docker_config=worker_config.docker,
            kubernetes_config=worker_config.kubernetes,
        )
        try:
            job_id = await backend.spawn_task(
                task_id=task_id,
                prompt_path=str(prompt_path.relative_to(workspace_root)),
                output_dir=str(output_dir.relative_to(workspace_root)),
                webhook_url=webhook_url,
                model=agent,
                timeout_minutes=timeout_minutes,
                context_paths=validated_context_paths,
                callback_token=callback_token,
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
            "agent": agent,
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

    if summary := task.get("summary"):
        result["summary"] = summary

    if error_message := task.get("error_message"):
        result["error_message"] = error_message

    if (exit_code := task.get("exit_code")) is not None:
        result["exit_code"] = exit_code

    # Include output files
    output_files = task.get("output_files") or []
    if output_files:
        result["output_files"] = output_files

        # Optionally include file contents
        if include_file_contents:
            workspace_root = get_workspace_root(exec_context)
            file_contents: dict[str, str | dict[str, str]] = {}

            for file_info in output_files:
                if isinstance(file_info, dict):
                    file_path = file_info.get("path", "")
                else:
                    file_path = str(file_info)

                if file_path:
                    try:
                        full_path = validate_workspace_path(file_path, workspace_root)
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

        if summary := task.get("summary"):
            task_info["summary"] = summary[:100] + ("..." if len(summary) > 100 else "")

        if error_message := task.get("error_message"):
            task_info["error"] = error_message[:100] + (
                "..." if len(error_message) > 100 else ""
            )

        task_list.append(task_info)

    return ToolResult(
        data={
            "tasks": task_list,
            "count": len(task_list),
            "conversation_id": exec_context.conversation_id,
        }
    )
