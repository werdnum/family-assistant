"""A2A (Agent-to-Agent) protocol endpoints.

Provides:
- GET /.well-known/agent.json - Agent Card discovery (legacy path)
- GET /.well-known/agent-card.json - Agent Card discovery (spec v0.3.0 path)
- POST /a2a - JSON-RPC 2.0 dispatch (message/send, message/stream, tasks/get, tasks/cancel)
- POST /a2a/stream - SSE streaming (message/stream, legacy separate endpoint)
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sse_starlette.sse import EventSourceResponse
from starlette.responses import Response

from family_assistant.a2a.attachments import (
    A2AAttachmentError,
    A2AAttachmentTransfer,
)
from family_assistant.a2a.converters import error_to_artifact, text_to_a2a_part
from family_assistant.a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    Artifact,
    JSONRPCError,
    JSONRPCErrorResponse,
    JSONRPCRequest,
    Message,
    MessageSendParams,
    Part,
    Role,
    Task,
    TaskArtifactUpdateEvent,
    TaskIdParams,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
)
from family_assistant.llm.content_parts import ContentPartDict
from family_assistant.processing import DelegatableService, ProcessingService
from family_assistant.security.taint import (
    A2A_TAINT_METADATA_KEY,
    SourceTrustTier,
    TaintMetadata,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
    coerce_taint_metadata,
)
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.a2a_tasks import A2ATaskRow
from family_assistant.web.dependencies import get_current_user, get_db

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.interfaces import ChatInterface
    from family_assistant.processing.types import ChatInteractionResult
    from family_assistant.services.attachment_registry import AttachmentRegistry
    from family_assistant.telegram.protocols import ConfirmationUIManager

logger = logging.getLogger(__name__)

a2a_router = APIRouter()
a2a_wellknown_router = APIRouter()

# ===== Helper: resolve processing service by profile =====


def _get_processing_services(request: Request) -> dict[str, DelegatableService]:
    """Get the processing services registry from app state."""
    registry: dict[str, DelegatableService] = getattr(
        request.app.state, "processing_services", {}
    )
    return registry


def _get_default_service(request: Request) -> ProcessingService | None:
    """Get the default processing service."""
    return getattr(request.app.state, "processing_service", None)


def _get_attachment_registry(request: Request) -> "AttachmentRegistry":
    """Get the attachment registry from app state.

    Required: attachment bytes crossing the A2A boundary are stored and read
    through it, so a deployment without one cannot serve A2A traffic correctly.
    """
    registry: AttachmentRegistry | None = getattr(
        request.app.state, "attachment_registry", None
    )
    if registry is None:
        raise RuntimeError("AttachmentRegistry is not configured on app state")
    return registry


def _get_a2a_cancel_events(request: Request) -> dict[str, asyncio.Event]:
    """Return the shared cancel-event registry, creating it if absent.

    Production initialises this on app state; the defensive create keeps test
    harnesses that build a bare app from needing to pre-seed it.
    """
    events: dict[str, asyncio.Event] | None = getattr(
        request.app.state, "a2a_cancel_events", None
    )
    if events is None:
        events = {}
        request.app.state.a2a_cancel_events = events
    return events


def _get_a2a_background_tasks(request: Request) -> "dict[str, asyncio.Task[None]]":
    """Return the shared background-send task registry, creating it if absent.

    Strong references to in-flight non-blocking sends are held here so the event
    loop does not garbage-collect them, and so ``tasks/cancel`` and shutdown can
    reach them.
    """
    tasks: dict[str, asyncio.Task[None]] | None = getattr(
        request.app.state, "a2a_background_tasks", None
    )
    if tasks is None:
        tasks = {}
        request.app.state.a2a_background_tasks = tasks
    return tasks


# ===== Agent Card Discovery =====


@a2a_wellknown_router.get("/.well-known/agent.json")
@a2a_wellknown_router.get("/.well-known/agent-card.json")
async def get_agent_card(request: Request) -> AgentCard:
    """Return the A2A Agent Card describing this server's capabilities."""
    registry = _get_processing_services(request)
    default_service = _get_default_service(request)

    skills: list[AgentSkill] = []
    for profile_id, service in registry.items():
        if service.kind == "remote":
            continue
        config = service.service_config
        assert isinstance(service, ProcessingService)  # filtered to local above
        tool_defs = await service.tools_provider.get_tool_definitions()
        tool_names = [d.get("function", {}).get("name", "unknown") for d in tool_defs]

        skills.append(
            AgentSkill(
                id=profile_id,
                name=profile_id,
                description=config.description or f"Profile: {profile_id}",
                tags=sorted(tool_names)[:10],
            )
        )

    base_url = str(request.base_url).rstrip("/")
    default_id = default_service.service_config.id if default_service else "assistant"

    return AgentCard(
        name=f"Family Assistant ({default_id})",
        description="Family Assistant AI agent with multiple service profiles",
        url=f"{base_url}/api/a2a",
        version="0.1.0",
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=False,
            state_transition_history=True,
        ),
        skills=skills,
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
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
    request_id: str | int | None,
    code: int,
    message: str,
) -> JSONResponse:
    content = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
    return JSONResponse(content=content)


def _jsonrpc_result(request_id: str | int | None, result: object) -> JSONResponse:
    content = {"jsonrpc": "2.0", "id": request_id, "result": result}
    return JSONResponse(content=content)


@a2a_router.post("")
async def a2a_jsonrpc(
    rpc_request: JSONRPCRequest,
    request: Request,
    current_user: Annotated[dict[str, object], Depends(get_current_user)],
    db_context: Annotated[Database, Depends(get_db)],
) -> Response:
    """JSON-RPC 2.0 endpoint for A2A protocol methods.

    Per A2A spec, both message/send and message/stream are dispatched to the
    same URL (the agent card's ``url`` field).
    """
    method = rpc_request.method
    params = rpc_request.params or {}
    request_id = rpc_request.id

    try:
        if method == "message/send":
            return await _handle_send_message(
                request_id, params, request, current_user, db_context
            )
        elif method == "message/stream":
            return await a2a_stream(rpc_request, request, current_user)
        elif method == "tasks/get":
            return await _handle_get_task(request_id, params, db_context)
        elif method == "tasks/cancel":
            return await _handle_cancel_task(request_id, params, request, db_context)
        else:
            return _jsonrpc_error(
                request_id, METHOD_NOT_FOUND, f"Unknown method: {method}"
            )
    except Exception:
        logger.exception("Error handling A2A JSON-RPC method %s", method)
        return _jsonrpc_error(request_id, INTERNAL_ERROR, "Internal server error")


# ===== message/send =====


def _send_is_blocking(send_params: MessageSendParams) -> bool:
    """Whether the client asked to block until the task is terminal.

    Per the A2A spec, ``MessageSendConfiguration.blocking`` defaults to true
    (synchronous) when unset, so an absent configuration preserves the
    historical synchronous behaviour. ``blocking=false`` opts into background
    processing: the server returns a non-terminal ``working`` task immediately
    and the client polls ``tasks/get`` until it is terminal.
    """
    config = send_params.configuration
    if config is None or config.blocking is None:
        return True
    return config.blocking


async def _handle_send_message(
    request_id: str | int | None,
    params: dict[str, object],
    request: Request,
    current_user: dict[str, object],
    db_context: Database,
) -> JSONResponse:
    """Handle the message/send JSON-RPC method.

    With ``configuration.blocking`` true (the default) the interaction runs
    inline and the terminal task is returned in this response. With
    ``blocking`` false the interaction runs in a background task and a
    non-terminal ``working`` task is returned immediately; the client then
    polls ``tasks/get`` (and may ``tasks/cancel``) until the task is terminal.
    """
    try:
        send_params = MessageSendParams.model_validate(params)
    except ValidationError as e:
        return _jsonrpc_error(request_id, INVALID_PARAMS, f"Invalid params: {e}")

    message = send_params.message
    task_id = message.task_id or str(uuid.uuid4())
    context_id = message.context_id or str(uuid.uuid4())
    conversation_id = f"a2a-{context_id}"

    service = _resolve_service(request, message)
    if service is None:
        return _jsonrpc_error(
            request_id, INVALID_PARAMS, "No processing service available"
        )

    profile_id = service.service_config.id
    user_id = str(current_user.get("user_identifier", "a2a_user"))
    attachment_registry = _get_attachment_registry(request)
    history_entry = message.model_dump(exclude_none=True)

    # Claim the task id before converting: conversion registers the peer's inline
    # files as durable attachments, and a retry that reuses a task id must not
    # store a second copy of every file only to be handed the existing task.
    # create_task_if_absent handles concurrent retries with the same task_id
    # atomically, returning the existing task rather than surfacing the
    # unique-constraint loser as a JSON-RPC internal error. The 'working' row is
    # durable when this returns, so a background task -- which runs on its own
    # connection -- can see it.
    existing = await db_context.a2a_tasks.create_task_if_absent(
        task_id=task_id,
        profile_id=profile_id,
        conversation_id=conversation_id,
        context_id=context_id,
        status=TaskState.working,
        history_json=[history_entry],
    )
    if existing is not None:
        return _jsonrpc_result(
            request_id, _row_to_task(existing).model_dump(exclude_none=True)
        )

    # Convert A2A message to FA content parts, registering any inline files
    # the peer sent as attachments owned by the authenticated caller. The task
    # row is already claimed, so a bad message finalizes it as failed rather
    # than leaving it 'working' forever.
    try:
        content_parts: list[ContentPartDict] = await A2AAttachmentTransfer(
            attachment_registry, db_context
        ).message_to_content_parts(
            message, conversation_id=conversation_id, owner_user_id=user_id
        )
        if not content_parts:
            raise ValueError("Message contained no processable content parts")
    except ValueError as e:
        await db_context.a2a_tasks.update_task_status(
            task_id=task_id,
            status=TaskState.failed,
            artifacts_json=[error_to_artifact(str(e)).model_dump(exclude_none=True)],
        )
        return _jsonrpc_error(request_id, INVALID_PARAMS, f"Invalid message parts: {e}")

    chat_interfaces = getattr(request.app.state, "chat_interfaces", None)
    confirmation_ui_managers = getattr(
        request.app.state,
        "confirmation_ui_managers",
        None,
    )
    base_url = str(request.base_url).rstrip("/")

    if not _send_is_blocking(send_params):
        return await _start_background_send(
            request_id,
            request=request,
            service=service,
            task_id=task_id,
            context_id=context_id,
            conversation_id=conversation_id,
            content_parts=content_parts,
            message=message,
            history_entry=history_entry,
            user_id=user_id,
            chat_interfaces=chat_interfaces,
            confirmation_ui_managers=confirmation_ui_managers,
            base_url=base_url,
            attachment_registry=attachment_registry,
        )

    task = await _execute_and_persist_send(
        db_context=db_context,
        service=service,
        task_id=task_id,
        context_id=context_id,
        conversation_id=conversation_id,
        content_parts=content_parts,
        message=message,
        history_entry=history_entry,
        user_id=user_id,
        chat_interfaces=chat_interfaces,
        confirmation_ui_managers=confirmation_ui_managers,
        base_url=base_url,
        attachment_registry=attachment_registry,
    )
    return _jsonrpc_result(request_id, task.model_dump(exclude_none=True))


async def _execute_and_persist_send(
    *,
    db_context: Database,
    service: DelegatableService,
    task_id: str,
    context_id: str,
    conversation_id: str,
    content_parts: list[ContentPartDict],
    message: Message,
    history_entry: dict[str, object],
    user_id: str,
    chat_interfaces: "dict[str, ChatInterface] | None",
    confirmation_ui_managers: "dict[str, ConfirmationUIManager] | None",
    base_url: str,
    attachment_registry: "AttachmentRegistry",
) -> Task:
    """Run the chat interaction and persist the terminal task; return it.

    Shared by the synchronous (blocking) send path and the background
    (non-blocking) send path. Takes its ``db_context`` as a parameter so the
    background path can supply a fresh, request-independent context.
    """
    result = await service.handle_chat_interaction(
        db_context=db_context,
        interface_type="a2a",
        conversation_id=conversation_id,
        trigger_content_parts=content_parts,
        trigger_interface_message_id=message.message_id,
        user_name=user_id,
        user_id=user_id,
        chat_interfaces=chat_interfaces,
        confirmation_ui_managers=confirmation_ui_managers,
        initial_taint_sources=_initial_taint_sources_from_message(message),
    )

    if result.has_error:
        artifact = error_to_artifact(result.error_traceback or "Unknown error")
        final_status = TaskState.failed
    else:
        artifact, final_status = await _artifact_for_result(
            result,
            attachment_registry=attachment_registry,
            db_context=db_context,
            user_id=user_id,
            base_url=base_url,
        )

    artifacts = [artifact] if artifact else []
    artifacts_dicts = [a.model_dump(exclude_none=True) for a in artifacts]

    response_parts = [text_to_a2a_part(result.text_reply)] if result.text_reply else []
    agent_message = Message(
        role=Role.agent,
        parts=response_parts or [Part(root=TextPart(text=""))],
        message_id=str(uuid.uuid4()),
        task_id=task_id,
        context_id=context_id,
    )
    history = [history_entry, agent_message.model_dump(exclude_none=True)]

    persisted = await db_context.a2a_tasks.update_task_status(
        task_id=task_id,
        status=final_status,
        artifacts_json=artifacts_dicts,
        history_json=history,
    )
    if not persisted:
        # The row went terminal while this send was running -- a concurrent
        # tasks/cancel, which the blocking path is now exposed to because its
        # 'working' row is durable the moment it is written rather than at the
        # end of the request. The guarded update above is what keeps ``canceled``
        # winning; returning the locally built Task would hand the caller a
        # completed task whose stored row says otherwise, so report what was
        # actually persisted.
        row = await db_context.a2a_tasks.get_task(task_id)
        if row is not None:
            logger.info(
                "A2A task %s reached a terminal state while its send was "
                "running; returning the persisted '%s' task.",
                task_id,
                row["status"],
            )
            return _row_to_task(row)
        logger.warning(
            "A2A task %s could not be finalized and its row is gone; "
            "returning the in-memory result.",
            task_id,
        )

    return Task(
        id=task_id,
        context_id=context_id,
        status=TaskStatus(state=final_status, message=agent_message),
        artifacts=artifacts if artifacts else None,
        history=[message, agent_message],
    )


async def _artifact_for_result(
    result: "ChatInteractionResult",
    *,
    attachment_registry: "AttachmentRegistry",
    db_context: Database,
    user_id: str,
    base_url: str,
) -> tuple[Artifact | None, TaskState]:
    """Build the response artifact, failing the task if a file cannot be sent.

    An attachment the turn queued but that cannot be read is a fault worth
    reporting: handing the peer a download URL it would be refused too just
    turns the failure into a dangling reference on a 'completed' task.
    """
    try:
        return (
            await A2AAttachmentTransfer(
                attachment_registry, db_context
            ).result_to_artifact(
                result,
                acting_user_id=user_id,
                attachment_urls=_attachment_urls(base_url, result.attachment_ids),
            ),
            TaskState.completed,
        )
    except A2AAttachmentError:
        logger.exception("A2A response attachment could not be sent")
        return (
            error_to_artifact("A response attachment could not be delivered"),
            TaskState.failed,
        )


async def _start_background_send(
    request_id: str | int | None,
    *,
    request: Request,
    service: DelegatableService,
    task_id: str,
    context_id: str,
    conversation_id: str,
    content_parts: list[ContentPartDict],
    message: Message,
    history_entry: dict[str, object],
    user_id: str,
    chat_interfaces: "dict[str, ChatInterface] | None",
    confirmation_ui_managers: "dict[str, ConfirmationUIManager] | None",
    base_url: str,
    attachment_registry: "AttachmentRegistry",
) -> JSONResponse:
    """Spawn background processing and return a non-terminal ``working`` task."""
    db_engine: AsyncEngine = request.app.state.database_engine
    cancel_events = _get_a2a_cancel_events(request)
    background_tasks = _get_a2a_background_tasks(request)

    cancel_event = asyncio.Event()
    cancel_events[task_id] = cancel_event

    background_tasks[task_id] = asyncio.create_task(
        _run_background_send(
            db_engine=db_engine,
            service=service,
            task_id=task_id,
            context_id=context_id,
            conversation_id=conversation_id,
            content_parts=content_parts,
            message=message,
            history_entry=history_entry,
            user_id=user_id,
            chat_interfaces=chat_interfaces,
            confirmation_ui_managers=confirmation_ui_managers,
            base_url=base_url,
            attachment_registry=attachment_registry,
            background_tasks=background_tasks,
            cancel_events=cancel_events,
        ),
        name=f"a2a-send-{task_id}",
    )

    # Let the new task reach its first suspension point before returning. A
    # task cancelled before its first step never runs its body at all, so its
    # CancelledError handler never persists a terminal state and the row would
    # be stuck 'working' forever -- and a graceful shutdown cancels in-flight
    # sends without warning.
    await asyncio.sleep(0)

    working_task = Task(
        id=task_id,
        context_id=context_id,
        status=TaskStatus(state=TaskState.working),
        history=[message],
    )
    return _jsonrpc_result(request_id, working_task.model_dump(exclude_none=True))


async def _run_background_send(
    *,
    db_engine: "AsyncEngine",
    service: DelegatableService,
    task_id: str,
    context_id: str,
    conversation_id: str,
    content_parts: list[ContentPartDict],
    message: Message,
    history_entry: dict[str, object],
    user_id: str,
    chat_interfaces: "dict[str, ChatInterface] | None",
    confirmation_ui_managers: "dict[str, ConfirmationUIManager] | None",
    base_url: str,
    attachment_registry: "AttachmentRegistry",
    background_tasks: "dict[str, asyncio.Task[None]]",
    cancel_events: dict[str, asyncio.Event],
) -> None:
    """Run a non-blocking send to terminal on its own db context.

    Persists the terminal task. ``update_task_status`` only transitions a
    non-terminal task, so a prior ``tasks/cancel`` (or the stale-row reaper)
    that already finalized this row wins over a later write here.
    """
    try:
        bg_db = Database(db_engine)
        await _execute_and_persist_send(
            db_context=bg_db,
            service=service,
            task_id=task_id,
            context_id=context_id,
            conversation_id=conversation_id,
            content_parts=content_parts,
            message=message,
            history_entry=history_entry,
            user_id=user_id,
            chat_interfaces=chat_interfaces,
            confirmation_ui_managers=confirmation_ui_managers,
            base_url=base_url,
            attachment_registry=attachment_registry,
        )
    except asyncio.CancelledError:
        # Cancelled by tasks/cancel (the DB row is already 'canceled') or by a
        # graceful shutdown (stop_services cancels in-flight sends, then awaits
        # them via gather). Persist a terminal 'canceled' state so a
        # shutdown-interrupted send does not leave the row 'working' forever —
        # after a restart there is no background work left to finish it, so
        # tasks/get would otherwise return a non-terminal task indefinitely. The
        # terminal-state CAS in update_task_status makes this a safe no-op when a
        # tasks/cancel already finalized the row. The write is awaited inline (not
        # shielded): the cancellation has already been delivered and caught, so
        # this completes on the normal single-cancel shutdown path before the
        # engine is disposed, without orphaning a detached task. A rare second
        # (forceful) cancel during the write propagates its CancelledError and
        # leaves the row 'working', recovered by the delegating client's cap.
        logger.info("Background A2A send %s cancelled.", task_id)
        try:
            await _mark_a2a_task_terminal(
                db_engine,
                task_id,
                TaskState.canceled,
                "Interrupted by server shutdown",
            )
        except Exception:
            logger.exception(
                "Failed to persist canceled state for A2A send %s.", task_id
            )
        raise
    except Exception:
        logger.exception("Background A2A send %s failed.", task_id)
        with suppress(Exception):
            await _mark_a2a_task_terminal(
                db_engine, task_id, TaskState.failed, "Internal error"
            )
    finally:
        background_tasks.pop(task_id, None)
        cancel_events.pop(task_id, None)


async def _mark_a2a_task_terminal(
    db_engine: "AsyncEngine",
    task_id: str,
    status: TaskState,
    error_text: str,
) -> None:
    """Best-effort write of a terminal status for a backgrounded a2a task."""
    artifact = error_to_artifact(error_text)
    db = Database(db_engine)
    await db.a2a_tasks.update_task_status(
        task_id=task_id,
        status=status,
        artifacts_json=[artifact.model_dump(exclude_none=True)] if artifact else [],
    )


# ===== tasks/get =====


async def _handle_get_task(
    request_id: str | int | None,
    params: dict[str, object],
    db_context: Database,
) -> JSONResponse:
    """Handle the tasks/get JSON-RPC method."""
    try:
        task_params = TaskIdParams.model_validate(params)
    except ValidationError as e:
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
    request_id: str | int | None,
    params: dict[str, object],
    request: Request,
    db_context: Database,
) -> JSONResponse:
    """Handle the tasks/cancel JSON-RPC method."""
    try:
        task_params = TaskIdParams.model_validate(params)
    except ValidationError as e:
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

    # Read the post-cancel row for the response BEFORE disturbing the background
    # task: hard-cancelling it tears down its own DB connection, so we finish
    # this request's DB work first.
    row = await db_context.a2a_tasks.get_task(task_params.id)
    if row is None:
        return _jsonrpc_error(
            request_id, TASK_NOT_FOUND, "Task disappeared after cancel"
        )

    # Signal cooperative cancellation to any running streaming generator, and
    # hard-cancel a background (non-blocking) send task if one is in flight. The
    # DB row was already set to ``canceled`` above, so the background task's
    # terminal write is a guarded no-op and ``canceled`` wins.
    cancel_events = _get_a2a_cancel_events(request)
    cancel_event = cancel_events.get(task_params.id)
    if cancel_event is not None:
        cancel_event.set()

    background_tasks = _get_a2a_background_tasks(request)
    background_task = background_tasks.get(task_params.id)
    if background_task is not None and not background_task.done():
        background_task.cancel()

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
        err = JSONRPCErrorResponse(
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
        send_params = MessageSendParams.model_validate(params)
    except ValidationError as e:
        validation_err = JSONRPCErrorResponse(
            id=rpc_request.id,
            error=JSONRPCError(code=INVALID_PARAMS, message=f"Invalid params: {e}"),
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


def _sse_jsonrpc(
    request_id: str | int | None, event_type: str, result: object
) -> dict[str, str]:
    """Wrap an A2A event in a JSON-RPC 2.0 response envelope for SSE."""
    envelope = {"jsonrpc": "2.0", "id": request_id, "result": result}
    return {"event": event_type, "data": json.dumps(envelope)}


async def _stream_message(
    request_id: str | int | None,
    send_params: MessageSendParams,
    request: Request,
    current_user: dict[str, object],
) -> AsyncIterator[dict[str, str]]:
    """Generate SSE events for a streaming A2A message interaction."""
    message = send_params.message
    task_id = message.task_id or str(uuid.uuid4())
    context_id = message.context_id or str(uuid.uuid4())
    conversation_id = f"a2a-{context_id}"

    service = _resolve_service(request, message)

    if service is None:
        event = TaskStatusUpdateEvent(
            task_id=task_id,
            context_id=context_id,
            status=TaskStatus(
                state=TaskState.failed,
                message=Message(
                    role=Role.agent,
                    parts=[Part(root=TextPart(text="No processing service available"))],
                    message_id=str(uuid.uuid4()),
                ),
            ),
            final=True,
        )
        yield _sse_jsonrpc(request_id, "status", event.model_dump(exclude_none=True))
        return

    profile_id = service.service_config.id
    user_id = str(current_user.get("user_identifier", "a2a_user"))
    db_engine = request.app.state.database_engine
    base_url = str(request.base_url).rstrip("/")
    try:
        content_parts: list[ContentPartDict] = await A2AAttachmentTransfer(
            _get_attachment_registry(request), Database(db_engine)
        ).message_to_content_parts(
            message, conversation_id=conversation_id, owner_user_id=user_id
        )
    except ValueError as e:
        event = TaskStatusUpdateEvent(
            task_id=task_id,
            context_id=context_id,
            status=TaskStatus(
                state=TaskState.failed,
                message=Message(
                    role=Role.agent,
                    parts=[Part(root=TextPart(text=f"Invalid message parts: {e}"))],
                    message_id=str(uuid.uuid4()),
                ),
            ),
            final=True,
        )
        yield _sse_jsonrpc(request_id, "status", event.model_dump(exclude_none=True))
        return
    if not content_parts:
        event = TaskStatusUpdateEvent(
            task_id=task_id,
            context_id=context_id,
            status=TaskStatus(
                state=TaskState.failed,
                message=Message(
                    role=Role.agent,
                    parts=[
                        Part(
                            root=TextPart(
                                text="Message contained no processable content parts"
                            )
                        )
                    ],
                    message_id=str(uuid.uuid4()),
                ),
            ),
            final=True,
        )
        yield _sse_jsonrpc(request_id, "status", event.model_dump(exclude_none=True))
        return

    history_entry = message.model_dump(exclude_none=True)

    # Create task in a short-lived context so it's immediately visible to
    # concurrent tasks/get and tasks/cancel requests.
    try:
        db_context = Database(db_engine)
        await db_context.a2a_tasks.create_task(
            task_id=task_id,
            profile_id=profile_id,
            conversation_id=conversation_id,
            context_id=context_id,
            status=TaskState.working,
            history_json=[history_entry],
        )
    except Exception:
        logger.exception("Failed to create A2A task %s", task_id)
        error_event = TaskStatusUpdateEvent(
            task_id=task_id,
            context_id=context_id,
            status=TaskStatus(
                state=TaskState.failed,
                message=Message(
                    role=Role.agent,
                    parts=[Part(root=TextPart(text="Failed to initialize task"))],
                    message_id=str(uuid.uuid4()),
                ),
            ),
            final=True,
        )
        yield _sse_jsonrpc(
            request_id, "status", error_event.model_dump(exclude_none=True)
        )
        return

    # Emit initial "working" status
    working_event = TaskStatusUpdateEvent(
        task_id=task_id,
        context_id=context_id,
        status=TaskStatus(state=TaskState.working),
        final=False,
    )
    yield _sse_jsonrpc(
        request_id, "status", working_event.model_dump(exclude_none=True)
    )

    # Stream the interaction with a separate DB context for ProcessingService
    accumulated_text = ""
    attachment_ids: list[str] | None = None
    has_error = False
    is_canceled = False
    error_msg = ""
    artifact_id = uuid.uuid4().hex

    # Register cancellation event so tasks/cancel can signal us
    cancel_events: dict[str, asyncio.Event] = request.app.state.a2a_cancel_events
    cancel_event = asyncio.Event()
    cancel_events[task_id] = cancel_event
    try:
        db_context = Database(db_engine)
        try:
            async for stream_event in service.handle_chat_interaction_stream(
                db_context=db_context,
                interface_type="a2a",
                conversation_id=conversation_id,
                trigger_content_parts=content_parts,
                trigger_interface_message_id=message.message_id,
                user_name=user_id,
                user_id=user_id,
                initial_taint_sources=_initial_taint_sources_from_message(message),
            ):
                # Check for cooperative cancellation between chunks
                if cancel_event.is_set():
                    is_canceled = True
                    break
                if stream_event.type == "content" and stream_event.content:
                    accumulated_text += stream_event.content
                    artifact_event = TaskArtifactUpdateEvent(
                        task_id=task_id,
                        context_id=context_id,
                        artifact=Artifact(
                            artifact_id=artifact_id,
                            parts=[Part(root=TextPart(text=stream_event.content))],
                        ),
                        append=True,
                    )
                    yield _sse_jsonrpc(
                        request_id,
                        "artifact",
                        artifact_event.model_dump(exclude_none=True),
                    )
                elif stream_event.type == "error":
                    has_error = True
                    error_msg = stream_event.error or "Unknown error"
                elif stream_event.type == "done":
                    # 'done' closes an agentic turn, not the interaction: a turn
                    # that called a tool emits one and keeps going, and the
                    # attachments a tool queued are only known to the last one.
                    # Stopping here would truncate every tool-using reply.
                    if stream_event.metadata:
                        attachment_ids = stream_event.metadata.get(
                            "attachment_ids", attachment_ids
                        )
        except Exception:
            logger.exception("Error during A2A streaming for task %s", task_id)
            has_error = True
            error_msg = "Internal streaming error"
    finally:
        cancel_events.pop(task_id, None)

    # Files the turn queued for its response are not part of the text stream, so
    # they are resolved here and ride out on the final artifact chunk.
    response_file_parts: list[Part] = []
    if attachment_ids and not has_error and not is_canceled:
        try:
            response_file_parts = await A2AAttachmentTransfer(
                _get_attachment_registry(request), Database(db_engine)
            ).response_attachment_parts(
                attachment_ids,
                acting_user_id=user_id,
                attachment_urls=_attachment_urls(base_url, attachment_ids),
            )
        except A2AAttachmentError:
            logger.exception("A2A response attachment could not be streamed")
            has_error = True
            error_msg = "A response attachment could not be delivered"

    # Emit final artifact chunk
    final_parts = (
        [Part(root=TextPart(text=accumulated_text))] if accumulated_text else []
    ) + response_file_parts
    if final_parts and not has_error and not is_canceled:
        final_artifact = TaskArtifactUpdateEvent(
            task_id=task_id,
            context_id=context_id,
            artifact=Artifact(
                artifact_id=artifact_id,
                name="response",
                parts=final_parts,
            ),
            last_chunk=True,
        )
        yield _sse_jsonrpc(
            request_id, "artifact", final_artifact.model_dump(exclude_none=True)
        )

    # Final status
    if is_canceled:
        final_status = TaskState.canceled
        status_message = Message(
            role=Role.agent,
            parts=[Part(root=TextPart(text="Task canceled"))],
            message_id=str(uuid.uuid4()),
        )
    elif has_error:
        final_status = TaskState.failed
        status_message = Message(
            role=Role.agent,
            parts=[Part(root=TextPart(text=error_msg))],
            message_id=str(uuid.uuid4()),
        )
    else:
        final_status = TaskState.completed
        status_message = Message(
            role=Role.agent,
            parts=[Part(root=TextPart(text=accumulated_text or ""))],
            message_id=str(uuid.uuid4()),
            task_id=task_id,
            context_id=context_id,
        )

    final_event = TaskStatusUpdateEvent(
        task_id=task_id,
        context_id=context_id,
        status=TaskStatus(state=final_status, message=status_message),
        final=True,
    )
    yield _sse_jsonrpc(request_id, "status", final_event.model_dump(exclude_none=True))

    # Persist final artifacts and history in a short-lived context
    artifacts_json: list[dict[str, object]] = []
    if has_error:
        err_art = error_to_artifact(error_msg)
        artifacts_json = [err_art.model_dump(exclude_none=True)]
    elif final_parts:
        art = Artifact(
            artifact_id=artifact_id,
            name="response",
            parts=final_parts,
        )
        artifacts_json = [art.model_dump(exclude_none=True)]

    history = [
        history_entry,
        status_message.model_dump(exclude_none=True),
    ]

    db_context = Database(db_engine)
    await db_context.a2a_tasks.update_task_status(
        task_id=task_id,
        status=final_status,
        artifacts_json=artifacts_json or None,
        history_json=history,
    )


# ===== Helpers =====


def _attachment_urls(
    base_url: str,
    attachment_ids: list[str] | None,
) -> dict[str, str]:
    """Build absolute download URLs for attachment IDs from a base URL.

    Takes a plain ``base_url`` rather than the request so background tasks can
    build URLs after the originating request has returned.
    """
    if not attachment_ids:
        return {}
    return {att_id: f"{base_url}/api/attachments/{att_id}" for att_id in attachment_ids}


def _resolve_service(request: Request, message: Message) -> ProcessingService | None:
    """Resolve which processing service to use for an A2A message."""
    registry = _get_processing_services(request)
    default_service = _get_default_service(request)

    profile_id = None
    if message.metadata and isinstance(message.metadata.get("profile"), str):
        profile_id = message.metadata["profile"]

    if profile_id and profile_id in registry:
        candidate = registry[profile_id]
        if isinstance(candidate, ProcessingService):
            return candidate
        return None  # remote profiles can't be served via A2A server
    if default_service:
        return default_service
    return None


def _initial_taint_sources_from_message(message: Message) -> tuple[TaintSource, ...]:
    """Restore FA runtime taint from A2A message metadata, when present."""
    if not message.metadata:
        return (_default_a2a_peer_taint_source(message),)
    raw_taint = message.metadata.get(A2A_TAINT_METADATA_KEY)
    taint_metadata: TaintMetadata | None = coerce_taint_metadata(raw_taint)
    if taint_metadata is None:
        return (_default_a2a_peer_taint_source(message),)
    return TurnTaintState.from_metadata(taint_metadata).sources


def _default_a2a_peer_taint_source(message: Message) -> TaintSource:
    return TaintSource(
        source_type=TaintSourceType.MANUAL,
        source_id=message.message_id,
        tier=SourceTrustTier.RECOGNIZED_MACHINE,
        labels=frozenset({"source_recognized_machine"}),
        reason=(
            "Inbound A2A message did not include Family Assistant runtime taint "
            "metadata; defaulting peer content to recognized_machine."
        ),
    )


def _row_to_task(row: A2ATaskRow) -> Task:
    """Convert a database row to an A2A Task object."""
    status_str = str(row.get("status", "submitted"))
    state = TaskState(status_str)

    artifacts = None
    raw_artifacts = row.get("artifacts_json")
    if isinstance(raw_artifacts, list):
        artifacts = [Artifact.model_validate(a) for a in raw_artifacts]

    history = None
    raw_history = row.get("history_json")
    if isinstance(raw_history, list):
        history = [Message.model_validate(m) for m in raw_history]

    context_id = (
        str(row["context_id"]) if row.get("context_id") else str(row.get("task_id", ""))
    )

    # Reconstruct status message from artifacts for terminal states
    status_message = None
    if state in {TaskState.completed, TaskState.failed} and artifacts:
        parts = [part for art in artifacts for part in art.parts]
        if parts:
            status_message = Message(
                role=Role.agent, parts=parts, message_id=str(uuid.uuid4())
            )

    return Task(
        id=str(row.get("task_id", "")),
        context_id=context_id,
        status=TaskStatus(state=state, message=status_message),
        artifacts=artifacts,
        history=history,
    )
