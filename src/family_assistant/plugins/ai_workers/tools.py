"""Tools for spawning and managing AI worker tasks."""

from __future__ import annotations

import logging
import secrets
import uuid
from typing import TYPE_CHECKING, Any

import aiofiles
import aiofiles.os

from family_assistant.plugins.ai_workers.instance import AIWorkersInstance
from family_assistant.plugins.ai_workers.lifecycle import TERMINAL_DB_STATUSES
from family_assistant.storage.events import WORKER_COMPLETION_EVENT_TYPE
from family_assistant.tools.confirmation_format import (
    confirmation_field,
    markdown_code_block,
)
from family_assistant.tools.metadata import (
    ToolConfirmation,
    ToolRegistration,
    ToolTag,
    make_local_tool_metadata,
)
from family_assistant.tools.types import ToolResult
from family_assistant.utils.workspace import get_workspace_root, validate_workspace_path

if TYPE_CHECKING:
    from collections.abc import Mapping

    from family_assistant.storage.database import DatabaseTransaction
    from family_assistant.tools.metadata import ToolImplementation
    from family_assistant.tools.types import (
        ToolArgumentsView,
        ToolDefinition,
        ToolExecutionContext,
    )

logger = logging.getLogger(__name__)


# Tool Definitions
WORKER_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "spawn_worker",
            "description": (
                "Spawn an isolated AI coding agent to handle a standalone task. "
                "The worker runs in a sandboxed container with access to the shared workspace "
                "and can use Claude Code or Gemini CLI to complete coding tasks.\n\n"
                "IMPORTANT: Workers have NO access to Family Assistant tools or data (no notes, "
                "calendar, documents, Home Assistant, etc.). They are raw coding agents only. "
                "For complex tasks that need Family Assistant context, delegate to the "
                "complex_tasks profile instead.\n\n"
                "Use this for:\n"
                "- Coding tasks requiring file manipulation in the shared workspace\n"
                "- Data processing or analysis of files\n"
                "- Tasks that need general-purpose computing (scripts, builds, etc.)\n"
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


NOT_CONFIGURED_ERROR = "AI workers are not configured for this profile"


def _worker_instance(exec_context: ToolExecutionContext) -> AIWorkersInstance | None:
    """The AI worker sandbox the turn's profile selected."""
    if exec_context.plugins is None:
        return None
    return exec_context.plugins.get(AIWorkersInstance)


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
    instance = _worker_instance(exec_context)
    if instance is None or exec_context.processing_service is None:
        return ToolResult(data={"error": NOT_CONFIGURED_ERROR})

    db_context = exec_context.db_context

    task = await db_context.worker_tasks.get_task(task_id)
    if not task:
        return ToolResult(data={"error": f"Task not found: {task_id}"})

    # Verify conversation access
    if task["conversation_id"] != exec_context.conversation_id:
        return ToolResult(
            data={"error": "Access denied: Task belongs to another conversation"}
        )

    if task["status"] in TERMINAL_DB_STATUSES:
        return ToolResult(
            data={
                "error": f"Task already in terminal state: {task['status']}",
                "task_id": task_id,
                "status": task["status"],
            }
        )

    # Cancel via backend if we have a job_name
    job_name = task.get("job_name")
    if job_name:
        backend = instance.backend(get_workspace_root(exec_context))
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
    instance = _worker_instance(exec_context)
    if instance is None or exec_context.processing_service is None:
        return ToolResult(data={"error": NOT_CONFIGURED_ERROR})

    app_config = exec_context.processing_service.app_config
    worker_config = instance.config

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

    async def spawn_worker() -> ToolResult:
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
                except ValueError as e:
                    skipped_context_paths.append({"path": path, "reason": str(e)})
                    logger.warning(f"Invalid context path {path}: {e}")
                    continue

                if await aiofiles.os.path.exists(validated):
                    validated_context_paths.append(path)
                else:
                    skipped_context_paths.append({
                        "path": path,
                        "reason": "does not exist",
                    })
                    logger.warning(f"Context path does not exist: {path}")

        # Build webhook URL (use configured URL or fall back to server_url)
        # Include event_type as query param so the worker doesn't need to know our event schema
        if worker_config.webhook_url:
            base_url = worker_config.webhook_url.rstrip("/")
        else:
            server_url = app_config.server_url.rstrip("/")
            base_url = f"{server_url}/webhook/event"
        separator = "&" if "?" in base_url else "?"
        webhook_url = f"{base_url}{separator}event_type={WORKER_COMPLETION_EVENT_TYPE}"

        # Generate callback token for webhook verification (32 bytes = 64 hex chars)
        callback_token = secrets.token_hex(32)

        async def _create_worker_with_listener(txn: DatabaseTransaction) -> None:
            await txn.worker_tasks.create_task(
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
            await txn.events.create_event_listener(
                name=f"worker-{task_id}-completion",
                source_id="webhook",
                match_conditions={
                    "event_type": WORKER_COMPLETION_EVENT_TYPE,
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

        await db_context.atomic(_create_worker_with_listener)

        backend = instance.backend(workspace_root)
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

        await db_context.worker_tasks.record_task_submission(
            task_id=task_id,
            job_name=job_id,
        )
        submitted_task = await db_context.worker_tasks.get_task(task_id)
        if submitted_task is None:
            raise RuntimeError(f"Worker task {task_id} disappeared after submission")
        current_status = submitted_task["status"]

        logger.info(f"Spawned worker task {task_id} with job {job_id}")

        # Build result with context path warnings if any were skipped
        # ast-grep-ignore: no-dict-any - Tool result data is dynamic
        result_data: dict[str, Any] = {
            "task_id": task_id,
            "status": current_status,
            "agent": agent,
            "timeout_minutes": timeout_minutes,
            "message": _worker_submission_message(task_id, current_status),
        }

        if skipped_context_paths:
            result_data["skipped_context_paths"] = skipped_context_paths
            result_data["warning"] = (
                f"{len(skipped_context_paths)} context path(s) were skipped due to errors. "
                "See skipped_context_paths for details."
            )

        return ToolResult(data=result_data)

    try:
        return await spawn_worker()
    except Exception as e:
        logger.exception(f"Failed to spawn worker task: {e}")
        return ToolResult(data={"error": f"Failed to spawn worker: {e!s}"})


def _worker_submission_message(task_id: str, status: str) -> str:
    """Describe the lifecycle state observed after backend submission."""
    if status == "running":
        return (
            f"Worker task '{task_id}' has started. "
            "You will be notified when it completes."
        )
    if status in TERMINAL_DB_STATUSES:
        return (
            f"Worker task '{task_id}' finished with status '{status}'. "
            f"Use read_task_result('{task_id}') to see the results."
        )
    return (
        f"Worker task '{task_id}' has been submitted. "
        "You will be notified when it completes."
    )


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
    if _worker_instance(exec_context) is None:
        return ToolResult(data={"error": NOT_CONFIGURED_ERROR})

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

                    async def read_output_file(
                        output_path: str,
                    ) -> str | dict[str, str]:
                        full_path = validate_workspace_path(output_path, workspace_root)
                        if await aiofiles.os.path.exists(full_path):
                            async with aiofiles.open(full_path) as f:
                                return await f.read()
                        return {"error": "File not found"}

                    try:
                        file_contents[file_path] = await read_output_file(file_path)
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
    if _worker_instance(exec_context) is None:
        return ToolResult(data={"error": NOT_CONFIGURED_ERROR})

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


def _spawn_worker_confirmation_prompt(arguments: Mapping[str, object]) -> str:
    """Build the full spawn_worker confirmation prompt."""
    task_description = str(arguments.get("task_description", "")).strip()

    fields = [
        confirmation_field("Agent", arguments.get("agent", "claude")),
        f"- Task description:\n{markdown_code_block(task_description)}",
    ]
    raw_context_paths = arguments.get("context_paths")
    if isinstance(raw_context_paths, (list, tuple)):
        if raw_context_paths:
            fields.append(
                confirmation_field(
                    "Context paths", ", ".join(str(path) for path in raw_context_paths)
                )
            )
    elif raw_context_paths is not None:
        # Script callers bypass JSON-schema validation, so a non-list value
        # (e.g. a mapping whose keys the tool would later iterate as paths)
        # must not be silently omitted from the prompt: the guard refuses the
        # call (see spawn_worker_block_reason), and the prompt says so.
        fields.append(
            f"- Context paths: ⚠️ Malformed value of type "
            f"{type(raw_context_paths).__name__} — context_paths must be an array of "
            "workspace path strings. The worker will not be launched."
        )
    fields.append(
        confirmation_field("Timeout (minutes)", arguments.get("timeout_minutes", 30))
    )
    return (
        "Do you want to launch an isolated AI coding worker? It executes code in a "
        "sandboxed container with network access — it can clone public git "
        "repositories, including this application's — but has no access to Family "
        "Assistant tools or data. It works from the task description below and "
        "returns output files:\n" + "\n".join(fields)
    )


async def render_spawn_worker_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
) -> str:
    """Render a confirmation prompt for launching an isolated AI coding worker."""
    _ = context
    return _spawn_worker_confirmation_prompt(args)


async def render_cancel_worker_task_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
) -> str:
    """Render a confirmation prompt for cancelling a worker task.

    Looks the task up so the approver sees what they are stopping, not just an
    opaque id. Mirrors cancel_worker_task_tool's conversation scoping: a task
    belonging to a different conversation is treated as not found, so the
    prompt never leaks another conversation's task details for a cancel that
    would be refused anyway. A profile without a sandbox is refused too, so
    its prompt shows no task details either.
    """
    task_id = str(args.get("task_id", "")).strip()
    fields = [confirmation_field("Task ID", task_id)]

    task = None
    db_context = getattr(context, "db_context", None)
    if task_id and db_context is not None and _worker_instance(context) is not None:
        task = await db_context.worker_tasks.get_task(task_id)
        if task is not None and task.get("conversation_id") != context.conversation_id:
            task = None

    if task is not None:
        fields.append(confirmation_field("Status", task.get("status")))
        fields.append(
            confirmation_field("Task description", task.get("task_description"))
        )
    else:
        fields.append(
            "- Task details: not found — the task may have already finished or the "
            "id may be wrong."
        )
    return "Do you want to *cancel* this worker task?\n" + "\n".join(fields)


def spawn_worker_block_reason(arguments: Mapping[str, object]) -> str | None:
    """Refuse context paths the confirmation prompt could not show.

    The context paths scope what the worker can read. Script callers bypass
    JSON-schema validation, so a present-but-non-list value is refused
    outright: the tool would later iterate it (a mapping's keys would become
    paths) while the prompt showed the approver no paths.
    """
    raw_context_paths = arguments.get("context_paths")
    if raw_context_paths is not None and not isinstance(
        raw_context_paths, (list, tuple)
    ):
        return (
            f"Error: context_paths must be an array of workspace path strings, "
            f"got {type(raw_context_paths).__name__}. Pass the paths as a JSON "
            'array (e.g. ["shared/data/input.csv"]).'
        )
    return None


def _tool(
    name: str,
    implementation: ToolImplementation,
    *tags: ToolTag,
    confirmation: ToolConfirmation | None = None,
) -> ToolRegistration:
    definitions = {
        definition["function"]["name"]: definition
        for definition in WORKER_TOOLS_DEFINITION
    }
    return ToolRegistration(
        definition=definitions[name],
        implementation=implementation,
        metadata=make_local_tool_metadata(tags),
        confirmation=confirmation,
    )


AI_WORKER_TOOLS: tuple[ToolRegistration, ...] = (
    _tool(
        "spawn_worker",
        spawn_worker_tool,
        ToolTag.CODE_EXECUTION,
        ToolTag.STATE_CHANGING,
        ToolTag.WORKER,
        ToolTag.OUTPUT_UNSPECIFIED,
        confirmation=ToolConfirmation(
            render=render_spawn_worker_confirmation,
            block_reason=spawn_worker_block_reason,
        ),
    ),
    _tool(
        "read_task_result",
        read_task_result_tool,
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.WORKER,
        ToolTag.OUTPUT_UNSPECIFIED,
    ),
    _tool(
        "cancel_worker_task",
        cancel_worker_task_tool,
        ToolTag.DESTRUCTIVE,
        ToolTag.STATE_CHANGING,
        ToolTag.WORKER,
        ToolTag.OUTPUT_TRUSTED,
        confirmation=ToolConfirmation(render=render_cancel_worker_task_confirmation),
    ),
    _tool(
        "list_worker_tasks",
        list_worker_tasks_tool,
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.WORKER,
        # OUTPUT_UNTRUSTED: returns stored task descriptions and results, which are
        # authored by whoever's content shaped the task.
        ToolTag.OUTPUT_UNTRUSTED,
    ),
)
