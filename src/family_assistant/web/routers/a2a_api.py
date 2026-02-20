"""A2A (Agent-to-Agent) protocol endpoints.

Provides:
- GET /.well-known/agent.json - Agent Card discovery
- POST /a2a - JSON-RPC 2.0 dispatch (message/send, tasks/get, tasks/cancel)
- POST /a2a/stream - SSE streaming (message/stream)
"""

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from family_assistant.a2a.converters import (
    a2a_message_to_content_parts,
    chat_result_to_artifact,
    content_parts_to_a2a_parts,
    error_to_artifact,
)
from family_assistant.a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    Artifact,
    JSONRPCError,
    JSONRPCRequest,
    JSONRPCResponse,
    Message,
    SendMessageParams,
    Task,
    TaskArtifactUpdateEvent,
    TaskIdParams,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
)
from family_assistant.llm.content_parts import ContentPartDict, text_content
from family_assistant.processing import ProcessingService
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.storage.repositories.a2a_tasks import A2ATaskRow
from family_assistant.web.dependencies import get_current_user, get_db

logger = logging.getLogger(__name__)

a2a_router = APIRouter()
a2a_wellknown_router = APIRouter()


# ===== Helper: resolve processing service by profile =====


def _get_processing_services(request: Request) -> dict[str, ProcessingService]:
    """Get the processing services registry from app state."""
    registry: dict[str, ProcessingService] = getattr(
        request.app.state, "processing_services", {}
    )
    return registry


def _get_default_service(request: Request) -> ProcessingService | None:
    """Get the default processing service."""
    return getattr(request.app.state, "processing_service", None)


# ===== Agent Card Discovery =====


@a2a_wellknown_router.get("/.well-known/agent.json")
async def get_agent_card(request: Request) -> AgentCard:
    """Return the A2A Agent Card describing this server's capabilities."""
    registry = _get_processing_services(request)
    default_service = _get_default_service(request)

    skills: list[AgentSkill] = []
    for profile_id, service in registry.items():
        config = service.service_config
        try:
            tool_defs = await service.tools_provider.get_tool_definitions()
            tool_names = [
                d.get("function", {}).get("name", "unknown") for d in tool_defs
            ]
        except Exception:
            logger.exception("Error fetching tools for profile %s", profile_id)
            tool_names = []

        skills.append(
            AgentSkill(
                id=profile_id,
                name=profile_id,
                description=config.description or f"Profile: {profile_id}",
                tags=sorted(tool_names[:10]),
            )
        )

    base_url = str(request.base_url).rstrip("/")
    default_id = default_service.service_config.id if default_service else "assistant"

    return AgentCard(
        name=f"Family Assistant ({default_id})",
        description="Family Assistant AI agent with multiple service profiles",
        url=f"{base_url}/api/a2a",
        capabilities=AgentCapabilities(
            streaming=True,
            pushNotifications=False,
            stateTransitionHistory=True,
        ),
        skills=skills,
        defaultInputModes=["text/plain"],
        defaultOutputModes=["text/plain"],
    )


# ===== JSON-RPC Dispatch =====

# Standard JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002


def _jsonrpc_error(
    request_id: str | int | None, code: int, message: str
) -> JSONResponse:
    resp = JSONRPCResponse(
        id=request_id, error=JSONRPCError(code=code, message=message)
    )
    return JSONResponse(content=resp.model_dump(exclude_none=True))


def _jsonrpc_result(request_id: str | int | None, result: object) -> JSONResponse:
    resp = JSONRPCResponse(id=request_id, result=result)
    return JSONResponse(content=resp.model_dump(exclude_none=True))


@a2a_router.post("")
async def a2a_jsonrpc(
    rpc_request: JSONRPCRequest,
    request: Request,
    current_user: Annotated[dict[str, object], Depends(get_current_user)],
    db_context: Annotated[DatabaseContext, Depends(get_db)],
) -> JSONResponse:
    """JSON-RPC 2.0 endpoint for A2A protocol methods."""
    method = rpc_request.method
    params = rpc_request.params or {}
    request_id = rpc_request.id

    try:
        if method == "message/send":
            return await _handle_send_message(
                request_id, params, request, current_user, db_context
            )
        elif method == "tasks/get":
            return await _handle_get_task(request_id, params, db_context)
        elif method == "tasks/cancel":
            return await _handle_cancel_task(request_id, params, db_context)
        else:
            return _jsonrpc_error(
                request_id, METHOD_NOT_FOUND, f"Unknown method: {method}"
            )
    except Exception:
        logger.exception("Error handling A2A JSON-RPC method %s", method)
        return _jsonrpc_error(request_id, INTERNAL_ERROR, "Internal server error")


# ===== message/send =====


async def _handle_send_message(
    request_id: str | int,
    params: dict[str, object],
    request: Request,
    current_user: dict[str, object],
    db_context: DatabaseContext,
) -> JSONResponse:
    """Handle the message/send JSON-RPC method."""
    try:
        send_params = SendMessageParams.model_validate(params)
    except Exception as e:
        return _jsonrpc_error(request_id, INVALID_PARAMS, f"Invalid params: {e}")

    message = send_params.message
    task_id = message.taskId or str(uuid.uuid4())
    context_id = message.contextId or str(uuid.uuid4())
    conversation_id = f"a2a-{context_id}"

    service = _resolve_service(request, message)
    if service is None:
        return _jsonrpc_error(
            request_id, INVALID_PARAMS, "No processing service available"
        )

    profile_id = service.service_config.id

    # Convert A2A message to FA content parts
    content_parts: list[ContentPartDict] = a2a_message_to_content_parts(message)
    if not content_parts:
        content_parts = [text_content("")]

    # Create task record
    user_id = str(current_user.get("user_identifier", "a2a_user"))
    history_entry = message.model_dump(exclude_none=True)

    await db_context.a2a_tasks.create_task(
        task_id=task_id,
        profile_id=profile_id,
        conversation_id=conversation_id,
        context_id=context_id,
        status=TaskState.WORKING,
        history_json=[history_entry],
    )

    # Execute the chat interaction
    result = await service.handle_chat_interaction(
        db_context=db_context,
        interface_type="a2a",
        conversation_id=conversation_id,
        trigger_content_parts=content_parts,
        trigger_interface_message_id=message.messageId,
        user_name=user_id,
        user_id=user_id,
    )

    # Build response
    if result.has_error:
        artifact = error_to_artifact(result.error_traceback or "Unknown error")
        final_status = TaskState.FAILED
    else:
        artifact = chat_result_to_artifact(result)
        final_status = TaskState.COMPLETED

    artifacts = [artifact] if artifact else []
    artifacts_dicts = [a.model_dump(exclude_none=True) for a in artifacts]

    # Build agent response message
    response_parts = (
        content_parts_to_a2a_parts([text_content(result.text_reply or "")])
        if result.text_reply
        else []
    )

    agent_message = Message(
        role="agent",
        parts=response_parts or [TextPart(text="")],
        messageId=str(uuid.uuid4()),
        taskId=task_id,
        contextId=context_id,
    )

    history = [history_entry, agent_message.model_dump(exclude_none=True)]

    await db_context.a2a_tasks.update_task_status(
        task_id=task_id,
        status=final_status,
        artifacts_json=artifacts_dicts,
        history_json=history,
    )

    task = Task(
        id=task_id,
        contextId=context_id,
        status=TaskStatus(
            state=final_status,
            message=agent_message,
        ),
        artifacts=artifacts if artifacts else None,
        history=[message, agent_message],
    )

    return _jsonrpc_result(request_id, task.model_dump(exclude_none=True))


# ===== tasks/get =====


async def _handle_get_task(
    request_id: str | int,
    params: dict[str, object],
    db_context: DatabaseContext,
) -> JSONResponse:
    """Handle the tasks/get JSON-RPC method."""
    try:
        task_params = TaskIdParams.model_validate(params)
    except Exception as e:
        return _jsonrpc_error(request_id, INVALID_PARAMS, f"Invalid params: {e}")

    row = await db_context.a2a_tasks.get_task(task_params.id)
    if row is None:
        return _jsonrpc_error(
            request_id, TASK_NOT_FOUND, f"Task not found: {task_params.id}"
        )

    task = _row_to_task(row)
    return _jsonrpc_result(request_id, task.model_dump(exclude_none=True))


# ===== tasks/cancel =====


async def _handle_cancel_task(
    request_id: str | int,
    params: dict[str, object],
    db_context: DatabaseContext,
) -> JSONResponse:
    """Handle the tasks/cancel JSON-RPC method."""
    try:
        task_params = TaskIdParams.model_validate(params)
    except Exception as e:
        return _jsonrpc_error(request_id, INVALID_PARAMS, f"Invalid params: {e}")

    canceled = await db_context.a2a_tasks.cancel_task(task_params.id)
    if not canceled:
        row = await db_context.a2a_tasks.get_task(task_params.id)
        if row is None:
            return _jsonrpc_error(
                request_id, TASK_NOT_FOUND, f"Task not found: {task_params.id}"
            )
        return _jsonrpc_error(
            request_id,
            TASK_NOT_CANCELABLE,
            f"Task is in state '{row['status']}' and cannot be canceled",
        )

    row = await db_context.a2a_tasks.get_task(task_params.id)
    if row is None:
        return _jsonrpc_error(
            request_id, TASK_NOT_FOUND, "Task disappeared after cancel"
        )

    task = _row_to_task(row)
    return _jsonrpc_result(request_id, task.model_dump(exclude_none=True))


# ===== message/stream (SSE) =====


@a2a_router.post("/stream")
async def a2a_stream(
    rpc_request: JSONRPCRequest,
    request: Request,
    current_user: Annotated[dict[str, object], Depends(get_current_user)],
) -> EventSourceResponse:
    """SSE streaming endpoint for A2A message/stream method."""
    if rpc_request.method != "message/stream":
        err = JSONRPCResponse(
            id=rpc_request.id,
            error=JSONRPCError(
                code=METHOD_NOT_FOUND,
                message=f"Streaming only supports message/stream, got: {rpc_request.method}",
            ),
        )

        async def error_gen() -> AsyncIterator[str]:
            yield json.dumps(err.model_dump(exclude_none=True))

        return EventSourceResponse(error_gen())

    params = rpc_request.params or {}
    try:
        send_params = SendMessageParams.model_validate(params)
    except Exception:
        validation_err = JSONRPCResponse(
            id=rpc_request.id,
            error=JSONRPCError(
                code=INVALID_PARAMS, message="Invalid params for message/stream"
            ),
        )

        async def validation_error_gen() -> AsyncIterator[str]:
            yield json.dumps(validation_err.model_dump(exclude_none=True))

        return EventSourceResponse(validation_error_gen())

    return EventSourceResponse(
        _stream_message(
            rpc_request.id,
            send_params,
            request,
            current_user,
        )
    )


async def _stream_message(
    request_id: str | int,
    send_params: SendMessageParams,
    request: Request,
    current_user: dict[str, object],
) -> AsyncIterator[dict[str, str]]:
    """Generate SSE events for a streaming A2A message interaction."""
    message = send_params.message
    task_id = message.taskId or str(uuid.uuid4())
    context_id = message.contextId or str(uuid.uuid4())
    conversation_id = f"a2a-{context_id}"

    service = _resolve_service(request, message)

    if service is None:
        event = TaskStatusUpdateEvent(
            taskId=task_id,
            contextId=context_id,
            status=TaskStatus(
                state=TaskState.FAILED,
                message=Message(
                    role="agent",
                    parts=[TextPart(text="No processing service available")],
                ),
            ),
            final=True,
        )
        yield {
            "event": "status",
            "data": json.dumps(event.model_dump(exclude_none=True)),
        }
        return

    profile_id = service.service_config.id
    content_parts: list[ContentPartDict] = a2a_message_to_content_parts(message)
    if not content_parts:
        content_parts = [text_content("")]

    user_id = str(current_user.get("user_identifier", "a2a_user"))

    # Create a fresh DB context for the SSE generator lifetime
    # (FastAPI Depends context managers exit before the generator runs)
    async with get_db_context(request.app.state.database_engine) as db_context:
        await db_context.a2a_tasks.create_task(
            task_id=task_id,
            profile_id=profile_id,
            conversation_id=conversation_id,
            context_id=context_id,
            status=TaskState.WORKING,
        )

        # Emit initial "working" status
        working_event = TaskStatusUpdateEvent(
            taskId=task_id,
            contextId=context_id,
            status=TaskStatus(state=TaskState.WORKING),
        )
        yield {
            "event": "status",
            "data": json.dumps(working_event.model_dump(exclude_none=True)),
        }

        # Stream the interaction
        accumulated_text = ""
        has_error = False
        error_msg = ""

        try:
            async for stream_event in service.handle_chat_interaction_stream(
                db_context=db_context,
                interface_type="a2a",
                conversation_id=conversation_id,
                trigger_content_parts=content_parts,
                trigger_interface_message_id=message.messageId,
                user_name=user_id,
                user_id=user_id,
            ):
                if stream_event.type == "content" and stream_event.content:
                    accumulated_text += stream_event.content
                    artifact_event = TaskArtifactUpdateEvent(
                        taskId=task_id,
                        contextId=context_id,
                        artifact=Artifact(
                            parts=[TextPart(text=stream_event.content)],
                            index=0,
                            append=True,
                        ),
                    )
                    yield {
                        "event": "artifact",
                        "data": json.dumps(
                            artifact_event.model_dump(exclude_none=True)
                        ),
                    }
                elif stream_event.type == "error":
                    has_error = True
                    error_msg = stream_event.error or "Unknown error"
                elif stream_event.type == "done":
                    break
        except Exception:
            logger.exception("Error during A2A streaming for task %s", task_id)
            has_error = True
            error_msg = "Internal streaming error"

        # Emit final artifact chunk
        if accumulated_text and not has_error:
            final_artifact = TaskArtifactUpdateEvent(
                taskId=task_id,
                contextId=context_id,
                artifact=Artifact(
                    name="response",
                    parts=[TextPart(text=accumulated_text)],
                    index=0,
                    lastChunk=True,
                ),
            )
            yield {
                "event": "artifact",
                "data": json.dumps(final_artifact.model_dump(exclude_none=True)),
            }

        # Final status
        if has_error:
            final_status = TaskState.FAILED
            status_message = Message(
                role="agent",
                parts=[TextPart(text=error_msg)],
            )
        else:
            final_status = TaskState.COMPLETED
            status_message = Message(
                role="agent",
                parts=[TextPart(text=accumulated_text or "")],
                messageId=str(uuid.uuid4()),
                taskId=task_id,
                contextId=context_id,
            )

        final_event = TaskStatusUpdateEvent(
            taskId=task_id,
            contextId=context_id,
            status=TaskStatus(state=final_status, message=status_message),
            final=True,
        )
        yield {
            "event": "status",
            "data": json.dumps(final_event.model_dump(exclude_none=True)),
        }

        # Update DB
        artifacts_json: list[dict[str, object]] = []
        if accumulated_text:
            art = Artifact(
                name="response",
                parts=[TextPart(text=accumulated_text)],
                index=0,
                lastChunk=True,
            )
            artifacts_json = [art.model_dump(exclude_none=True)]

        await db_context.a2a_tasks.update_task_status(
            task_id=task_id,
            status=final_status,
            artifacts_json=artifacts_json or None,
        )


# ===== Helpers =====


def _resolve_service(request: Request, message: Message) -> ProcessingService | None:
    """Resolve which processing service to use for an A2A message."""
    registry = _get_processing_services(request)
    default_service = _get_default_service(request)

    profile_id = None
    if message.metadata and isinstance(message.metadata.get("profile"), str):
        profile_id = message.metadata["profile"]

    if profile_id and profile_id in registry:
        return registry[profile_id]
    if default_service:
        return default_service
    return None


def _row_to_task(row: A2ATaskRow) -> Task:
    """Convert a database row to an A2A Task object."""
    status_str = str(row.get("status", "submitted"))
    try:
        state = TaskState(status_str)
    except ValueError:
        state = TaskState.SUBMITTED

    artifacts = None
    raw_artifacts = row.get("artifacts_json")
    if isinstance(raw_artifacts, list):
        artifacts = [Artifact.model_validate(a) for a in raw_artifacts]

    history = None
    raw_history = row.get("history_json")
    if isinstance(raw_history, list):
        history = [Message.model_validate(m) for m in raw_history]

    # Reconstruct status message from artifacts for terminal states
    status_message = None
    if state in {TaskState.COMPLETED, TaskState.FAILED} and artifacts:
        parts = [part for art in artifacts for part in art.parts]
        if parts:
            status_message = Message(role="agent", parts=parts)

    return Task(
        id=str(row.get("task_id", "")),
        contextId=str(row["context_id"]) if row.get("context_id") else None,
        status=TaskStatus(state=state, message=status_message),
        artifacts=artifacts,
        history=history,
    )
