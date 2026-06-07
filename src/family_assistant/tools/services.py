"""Service delegation tools.

This module contains tools for delegating requests to other specialized
assistant profiles (services).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

from family_assistant.llm.content_parts import attachment_content, text_content
from family_assistant.tools.types import (
    ConfirmationOutcome,
    ToolAttachment,
    ToolDefinition,
    ToolResult,
)

if TYPE_CHECKING:
    from family_assistant.llm.content_parts import ContentPartDict
    from family_assistant.storage.repositories.delegation_runs import (
        DelegationRunDict,
        DelegationRunSummary,
    )
    from family_assistant.tools.types import ToolExecutionContext


logger = logging.getLogger(__name__)

DELEGATED_PROFILE_RUN_TASK_TYPE = "delegated_profile_run"
_TERMINAL_DELEGATION_STATUSES = {"completed", "failed", "cancelled"}


def _delegation_confirmation_outcome_result(
    target_service_id: str,
    outcome: ConfirmationOutcome,
) -> ToolResult:
    """Convert a durable delegation confirmation outcome into a tool result."""
    if outcome.kind == "approved":
        return ToolResult(
            text=(
                f"Error: Confirmation for delegation to service "
                f"'{target_service_id}' was approved but not executed."
            ),
            attachments=None,
        )
    if outcome.kind == "completed":
        if isinstance(outcome.result, ToolResult):
            return outcome.result
        return ToolResult(text=str(outcome.result or ""), attachments=None)
    if outcome.kind == "failed":
        if isinstance(outcome.result, ToolResult):
            return outcome.result
        return ToolResult(
            text=str(
                outcome.result
                or f"Error: Failed to delegate task to service '{target_service_id}'."
            ),
            attachments=None,
        )
    if outcome.kind == "timed_out":
        return ToolResult(
            text=f"Error: Confirmation timed out for delegating to '{target_service_id}'.",
            attachments=None,
        )
    return ToolResult(
        text=f"OK. Delegation to service '{target_service_id}' cancelled by user.",
        attachments=None,
    )


def _now(exec_context: ToolExecutionContext) -> datetime:
    clock = exec_context.clock
    if clock is None:
        return datetime.now(UTC)
    return clock.now()


def _resolve_handoff_wait_seconds(
    exec_context: ToolExecutionContext,
    handoff_after_seconds: float | None,
    delivery_hint: Literal["auto", "background"],
) -> float:
    service = exec_context.processing_service
    tools_config = service.service_config.tools_config if service else None
    configured_wait = float(
        getattr(tools_config, "delegate_handoff_after_seconds", 15.0)
    )
    max_wait = float(getattr(tools_config, "delegate_handoff_max_seconds", 120.0))
    requested_wait = configured_wait
    if handoff_after_seconds is not None:
        requested_wait = handoff_after_seconds
    if delivery_hint == "background":
        requested_wait = 0.0
    return max(0.0, min(float(requested_wait), max_wait))


def _delegation_status_poll_seconds(exec_context: ToolExecutionContext) -> float:
    service = exec_context.processing_service
    tools_config = service.service_config.tools_config if service else None
    configured_poll = float(getattr(tools_config, "delegate_status_poll_seconds", 0.25))
    return max(0.05, configured_poll)


def _terminal_delegation_run(run: DelegationRunDict | None) -> bool:
    return run is not None and run["status"] in _TERMINAL_DELEGATION_STATUSES


async def _load_delegation_run(
    exec_context: ToolExecutionContext, delegation_id: str
) -> DelegationRunDict | None:
    async with exec_context.db_context.create_isolated_context() as isolated_db:
        return await isolated_db.delegation_runs.get_by_delegation_id(delegation_id)


async def _wait_for_delegation_run(
    exec_context: ToolExecutionContext,
    *,
    delegation_id: str,
    wait_seconds: float,
) -> DelegationRunDict | None:
    if wait_seconds <= 0:
        return await _load_delegation_run(exec_context, delegation_id)

    deadline = asyncio.get_running_loop().time() + wait_seconds
    poll_seconds = _delegation_status_poll_seconds(exec_context)
    while True:
        run = await _load_delegation_run(exec_context, delegation_id)
        if _terminal_delegation_run(run):
            return run

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return run
        await asyncio.sleep(min(poll_seconds, remaining))


async def _delegated_attachment_refs(
    exec_context: ToolExecutionContext,
    *,
    target_service_id: str,
    response_attachment_ids: list[str],
) -> list[ToolAttachment] | None:
    if not response_attachment_ids or not exec_context.attachment_registry:
        return None

    delegated_attachments: list[ToolAttachment] = []
    for attachment_id in response_attachment_ids:
        try:
            att_metadata = await exec_context.attachment_registry.get_attachment(
                exec_context.db_context, attachment_id
            )
            if att_metadata:
                delegated_attachments.append(
                    ToolAttachment(
                        mime_type=att_metadata.mime_type,
                        content=None,
                        attachment_id=attachment_id,
                        description=att_metadata.description
                        or f"Attachment from delegated service '{target_service_id}'",
                    )
                )
            else:
                logger.warning(
                    "Could not fetch metadata for delegated attachment %s",
                    attachment_id,
                )
                delegated_attachments.append(
                    ToolAttachment(
                        mime_type="application/octet-stream",
                        content=None,
                        attachment_id=attachment_id,
                        description=f"Attachment from delegated service '{target_service_id}'",
                    )
                )
        except Exception as exc:
            logger.error(
                "Error fetching delegated attachment metadata for %s: %s",
                attachment_id,
                exc,
                exc_info=True,
            )
            delegated_attachments.append(
                ToolAttachment(
                    mime_type="application/octet-stream",
                    content=None,
                    attachment_id=attachment_id,
                    description=f"Attachment from delegated service '{target_service_id}'",
                )
            )

    logger.info(
        "Propagating %d attachment(s) from delegated service",
        len(delegated_attachments),
    )
    return delegated_attachments


async def _completed_delegation_result(
    exec_context: ToolExecutionContext,
    *,
    target_service_id: str,
    run: DelegationRunDict,
) -> ToolResult:
    final_text_reply = run["result_text"]
    response_attachment_ids = run["result_attachment_ids_json"] or []

    if not final_text_reply:
        logger.info(
            "Delegated service '%s' returned no textual reply.",
            target_service_id,
        )
        return ToolResult(
            text=f"Service '{target_service_id}' processed the request but provided no textual response.",
            attachments=None,
        )

    delegated_attachments = await _delegated_attachment_refs(
        exec_context,
        target_service_id=target_service_id,
        response_attachment_ids=response_attachment_ids,
    )
    return ToolResult(text=final_text_reply, attachments=delegated_attachments)


def _delegation_reference_text(
    *, delegation_id: str, target_service_id: str, status: str
) -> str:
    return (
        "Delegation is still running.\n"
        f"Reference: {delegation_id}\n"
        f"Target profile: {target_service_id}\n"
        f"Status: {status}\n"
        "The conversation will be notified when it finishes."
    )


def _format_delegation_summary(summary: DelegationRunSummary) -> str:
    return json.dumps(summary, indent=2, default=str)


# Tool Definitions
SERVICE_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "delegate_to_service",
            "description": (
                "Delegates a specific user request to another specialized assistant profile (service) "
                "that might have different tools or capabilities. Use this if the main assistant "
                "cannot handle a request directly or if a specialized profile is more appropriate "
                "for the task. Profile-to-profile delegation controls are enforced by the "
                "tool policy engine.\n\n"
                "Available service profiles:\n{available_service_profiles_with_descriptions}\n\n"
                "Returns: A string containing the delegated service's response or an error message. "
                "On successful delegation, returns the text response from the target service. "
                "If service returns no text, returns 'Service [id] processed the request but provided no textual response.'. "
                "If service registry unavailable, returns 'Error: Service registry is not available to delegate the task.'. "
                "If target service not found, returns 'Error: Target service profile [id] not found.'. "
                "If delegation blocked by security policy, returns 'Error: Tool \\'delegate_to_service\\' is not allowed. [reason]'. "
                "If confirmation required but unavailable, returns 'Error: Confirmation required to delegate to [id], but no confirmation mechanism is available.'. "
                "If user cancels confirmation, returns 'OK. Delegation to service [id] cancelled by user.'. "
                "If confirmation times out, returns 'Error: Confirmation timed out for delegating to [id].'. "
                "If the delegated profile is still running after the handoff deadline, returns an async reference ID and the conversation will be notified when it finishes. "
                "On delegation error, returns 'Error: Failed to delegate task to service [id]. Details: [error]' or 'Error from [id] service: An error occurred during processing.'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_service_id": {
                        "type": "string",
                        "description": "The unique ID of the target service profile to delegate the request to (e.g., 'browser_profile').",
                    },
                    "user_request": {
                        "type": "string",
                        "description": "The specific request, question, or prompt to be processed by the target service.",
                    },
                    "confirm_delegation": {
                        "type": "boolean",
                        "description": "Optional. If true, explicitly ask the user for confirmation before delegating the task. Defaults to false. Policy-level confirmation requirements may also apply.",
                        "default": False,
                    },
                    "attachment_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of attachment UUIDs to include with the delegated request. These attachments must be accessible in the current conversation and will be passed to the target service for processing.",
                    },
                    "handoff_after_seconds": {
                        "type": "number",
                        "description": "Optional. Override how long to wait for an inline result before returning an async reference. Clamped by service configuration.",
                    },
                    "delivery_hint": {
                        "type": "string",
                        "enum": ["auto", "background"],
                        "description": "Optional. Use 'background' to return an async reference immediately; otherwise 'auto' waits briefly for a fast inline result.",
                        "default": "auto",
                    },
                },
                "required": ["target_service_id", "user_request"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_delegation_status",
            "description": (
                "Returns the status and available result for an asynchronous profile delegation reference. "
                "Use this for references returned by delegate_to_service, not for spawn_worker task IDs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "delegation_id": {
                        "type": "string",
                        "description": "The delegation reference ID returned by delegate_to_service, e.g. delegation_abc123.",
                    },
                },
                "required": ["delegation_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_delegations",
            "description": (
                "Lists recent asynchronous profile delegations for the current conversation. "
                "This is distinct from list_worker_tasks, which lists spawn_worker jobs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Optional status filter such as queued, running, completed, failed, or cancelled.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Optional maximum number of delegation runs to return. Defaults to 10.",
                        "default": 10,
                    },
                },
                "required": [],
            },
        },
    },
]


# Tool Implementations
async def delegate_to_service_tool(
    exec_context: ToolExecutionContext,
    target_service_id: str,
    user_request: str,
    confirm_delegation: bool = False,
    attachment_ids: list[str] | None = None,
    handoff_after_seconds: float | None = None,
    delivery_hint: Literal["auto", "background"] = "auto",
) -> ToolResult:
    """
    Delegates a user request to another specialized assistant profile (service).

    Args:
        exec_context: The execution context
        target_service_id: ID of the target service profile to delegate to
        user_request: The request text to delegate
        confirm_delegation: Whether to ask for user confirmation
        attachment_ids: Optional list of attachment UUIDs to include with the request
        handoff_after_seconds: Optional per-call handoff timeout override
        delivery_hint: Use background to return an async reference immediately

    Returns:
        ToolResult with response text from the target service and any attachments it generated
    """
    logger.info(
        f"Executing delegate_to_service_tool: target='{target_service_id}', request='{user_request[:50]}...', confirm={confirm_delegation}"
    )

    if (
        not exec_context.processing_service
        or not exec_context.processing_service.processing_services_registry
    ):
        logger.error(
            "Processing services registry not available in the current execution context."
        )
        return ToolResult(
            text="Error: Service registry is not available to delegate the task.",
            attachments=None,
        )

    registry = exec_context.processing_service.processing_services_registry
    target_service = registry.get(target_service_id)

    if not target_service:
        logger.error(
            f"Target service profile ID '{target_service_id}' not found in the registry."
        )
        return ToolResult(
            text=f"Error: Target service profile '{target_service_id}' not found.",
            attachments=None,
        )

    source_service_id = exec_context.processing_service.service_config.id
    allowed_sources = getattr(
        target_service.service_config,
        "allowed_delegation_sources",
        None,
    )
    if allowed_sources is not None and source_service_id not in allowed_sources:
        logger.warning(
            "Delegation from '%s' to '%s' blocked by target allowed_delegation_sources.",
            source_service_id,
            target_service_id,
        )
        return ToolResult(
            text=(
                "Error: Tool 'delegate_to_service' is not allowed. "
                f"Profile '{source_service_id}' is not permitted to delegate "
                f"to '{target_service_id}'."
            ),
            attachments=None,
        )

    confirmation_timeout_seconds = exec_context.processing_service.service_config.tools_config.confirmation_timeout_seconds
    actual_confirm_delegation = confirm_delegation

    if actual_confirm_delegation:
        if not exec_context.request_confirmation_callback:
            logger.error(
                f"Confirmation required for delegating to '{target_service_id}', but no confirmation callback is available. Aborting delegation."
            )
            return ToolResult(
                text=f"Error: Confirmation required to delegate to '{target_service_id}', but no confirmation mechanism is available.",
                attachments=None,
            )
        else:
            try:
                confirmation_outcome = await exec_context.request_confirmation_callback(
                    interface_type=exec_context.interface_type,
                    conversation_id=exec_context.conversation_id,
                    turn_id=exec_context.turn_id,
                    tool_name="delegate_to_service",
                    call_id=f"delegate_to_service_{uuid.uuid4()}",
                    tool_args={
                        "target_service_id": target_service_id,
                        "user_request": user_request,
                        "confirm_delegation": actual_confirm_delegation,
                        **(
                            {"attachment_ids": attachment_ids}
                            if attachment_ids is not None
                            else {}
                        ),
                    },
                    timeout_seconds=confirmation_timeout_seconds,
                    context=exec_context,
                )
                if confirmation_outcome.kind != "approved":
                    return _delegation_confirmation_outcome_result(
                        target_service_id,
                        confirmation_outcome,
                    )
            except TimeoutError:
                logger.warning(
                    f"Confirmation for delegating to '{target_service_id}' timed out."
                )
                return ToolResult(
                    text=f"Error: Confirmation timed out for delegating to '{target_service_id}'.",
                    attachments=None,
                )
            except Exception as e:
                logger.error(
                    f"Error during confirmation for delegating to '{target_service_id}': {e}",
                    exc_info=True,
                )
                return ToolResult(
                    text=f"Error during confirmation for delegating to '{target_service_id}': {e}",
                    attachments=None,
                )

    # Process attachments if provided
    content_parts: list[ContentPartDict] = [text_content(user_request)]

    if attachment_ids:
        if not exec_context.attachment_registry:
            logger.warning(
                "Attachment IDs provided but AttachmentRegistry not available - ignoring attachments"
            )
        else:
            attachment_registry = exec_context.attachment_registry

            for attachment_id in attachment_ids:
                try:
                    # Validate attachment exists and is accessible
                    attachment = await attachment_registry.get_attachment(
                        exec_context.db_context, attachment_id
                    )

                    if not attachment:
                        logger.warning(
                            f"Attachment {attachment_id} not found - skipping"
                        )
                        continue

                    content_parts.append(attachment_content(attachment_id))
                    logger.debug(f"Added attachment {attachment_id} to delegation")

                except Exception as e:
                    logger.error(f"Error validating attachment {attachment_id}: {e}")
                    continue

    if delivery_hint not in {"auto", "background"}:
        return ToolResult(
            text="Error: delivery_hint must be 'auto' or 'background'.",
            attachments=None,
        )

    delegation_id = f"delegation_{uuid.uuid4().hex}"
    task_id = f"{DELEGATED_PROFILE_RUN_TASK_TYPE}_{uuid.uuid4().hex}"
    subconversation_id = str(uuid.uuid4())
    wait_seconds = _resolve_handoff_wait_seconds(
        exec_context,
        handoff_after_seconds,
        delivery_hint,
    )
    handoff_after_at = _now(exec_context) + timedelta(seconds=wait_seconds)

    logger.info(
        "Enqueuing delegated request to service profile '%s' with %d content parts "
        "(delegation_id=%s, subconversation_id=%s, wait_seconds=%.2f)",
        target_service_id,
        len(content_parts),
        delegation_id,
        subconversation_id,
        wait_seconds,
    )
    try:
        async with exec_context.db_context.create_isolated_context() as isolated_db:
            await isolated_db.delegation_runs.create_run({
                "delegation_id": delegation_id,
                "task_id": task_id,
                "source_profile_id": source_service_id,
                "target_service_id": target_service_id,
                "interface_type": exec_context.interface_type,
                "conversation_id": exec_context.conversation_id,
                "user_id": exec_context.user_id,
                "user_name": exec_context.user_name,
                "source_turn_id": exec_context.turn_id,
                "source_tool_call_id": None,
                "subconversation_id": subconversation_id,
                "request_text": user_request,
                "content_parts_json": content_parts,
                "attachment_ids_json": list(attachment_ids or []),
                "handoff_after_at": handoff_after_at,
            })
            await isolated_db.tasks.enqueue(
                task_id=task_id,
                task_type=DELEGATED_PROFILE_RUN_TASK_TYPE,
                payload={
                    "delegation_id": delegation_id,
                    "interface_type": exec_context.interface_type,
                    "conversation_id": exec_context.conversation_id,
                    "user_name": exec_context.user_name,
                },
                max_retries_override=1,
            )

        run = await _wait_for_delegation_run(
            exec_context,
            delegation_id=delegation_id,
            wait_seconds=wait_seconds,
        )
        if run is not None and run["status"] == "completed":
            logger.info(
                "Delegated service '%s' completed inline for %s.",
                target_service_id,
                delegation_id,
            )
            return await _completed_delegation_result(
                exec_context,
                target_service_id=target_service_id,
                run=run,
            )
        if run is not None and run["status"] == "failed":
            logger.error(
                "Delegated service '%s' failed inline for %s: %s",
                target_service_id,
                delegation_id,
                run["error"],
            )
            return ToolResult(
                text=f"Error from '{target_service_id}' service: An error occurred during processing.",
                attachments=None,
            )

        async with exec_context.db_context.create_isolated_context() as isolated_db:
            await isolated_db.delegation_runs.mark_handed_off(
                delegation_id,
                _now(exec_context),
            )

        run_status = run["status"] if run is not None else "queued"
        return ToolResult(
            text=_delegation_reference_text(
                delegation_id=delegation_id,
                target_service_id=target_service_id,
                status=run_status,
            ),
            attachments=None,
            data={
                "delegation_id": delegation_id,
                "target_service_id": target_service_id,
                "status": run_status,
            },
        )

    except Exception as e:
        logger.error(
            f"Failed to delegate request to service '{target_service_id}': {e}",
            exc_info=True,
        )
        return ToolResult(
            text=f"Error: Failed to delegate task to service '{target_service_id}'. Details: {e}",
            attachments=None,
        )


async def get_delegation_status_tool(
    exec_context: ToolExecutionContext,
    delegation_id: str,
) -> ToolResult:
    """Return status for an asynchronous profile delegation."""
    run = await exec_context.db_context.delegation_runs.get_by_delegation_id(
        delegation_id
    )
    if run is None or (
        run["conversation_id"] != exec_context.conversation_id
        or run["interface_type"] != exec_context.interface_type
    ):
        return ToolResult(
            text=f"Error: Delegation '{delegation_id}' not found in this conversation.",
            attachments=None,
        )

    summary = exec_context.db_context.delegation_runs.summarize_run(run)
    return ToolResult(
        text=_format_delegation_summary(summary),
        data=cast("dict[str, Any]", summary),
    )


async def list_delegations_tool(
    exec_context: ToolExecutionContext,
    status: str | None = None,
    limit: int = 10,
) -> ToolResult:
    """List recent asynchronous profile delegations for the current conversation."""
    runs = await exec_context.db_context.delegation_runs.list_for_conversation(
        conversation_id=exec_context.conversation_id,
        interface_type=exec_context.interface_type,
        status=status,
        limit=limit,
    )
    summaries = [
        exec_context.db_context.delegation_runs.summarize_run(run) for run in runs
    ]
    if not summaries:
        status_text = f" with status '{status}'" if status else ""
        return ToolResult(
            text=f"No delegations found for this conversation{status_text}.",
            data=[],
        )
    return ToolResult(
        text=json.dumps(summaries, indent=2, default=str),
        data=summaries,
    )
