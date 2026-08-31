"""Service delegation tools.

This module contains tools for delegating requests to other specialized
assistant profiles (services).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from sqlalchemy.exc import IntegrityError

from family_assistant.llm.content_parts import attachment_content, text_content
from family_assistant.security.taint import TaintMetadata, TaintSource, TurnTaintState
from family_assistant.services.tool_call_review import (
    TriggerReviewInput,
    build_delegation_review_trigger,
)
from family_assistant.storage.delegation_runs import TERMINAL_DELEGATION_STATUSES
from family_assistant.tools.confirmation import (
    MAX_DELEGATION_REQUEST_CHARS,
    over_length_delegation_block_reason,
)
from family_assistant.tools.types import (
    ConfirmationOutcome,
    ToolArguments,
    ToolAttachment,
    ToolDefinition,
    ToolResult,
)
from family_assistant.utils.clock import SystemClock

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from family_assistant.config_models import ToolsConfig
    from family_assistant.llm.content_parts import ContentPartDict
    from family_assistant.processing.protocol import DelegatableService
    from family_assistant.storage.database import DatabaseTransaction
    from family_assistant.storage.repositories.delegation_runs import (
        DelegationRunDict,
        DelegationRunSummary,
    )
    from family_assistant.tools.types import (
        ToolConfirmationAuthorization,
        ToolExecutionContext,
    )


logger = logging.getLogger(__name__)

DELEGATED_PROFILE_RUN_TASK_TYPE = "delegated_profile_run"


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
    return (exec_context.clock or SystemClock()).now()


def _tools_config(exec_context: ToolExecutionContext) -> ToolsConfig:
    service = exec_context.processing_service
    if service is not None:
        return service.service_config.tools_config
    from family_assistant.config_models import ToolsConfig  # noqa: PLC0415

    return ToolsConfig()


def _resolve_handoff_wait_seconds(
    exec_context: ToolExecutionContext,
    handoff_after_seconds: float | None,
    delivery_hint: Literal["auto", "background"],
) -> float:
    tools_config = _tools_config(exec_context)
    requested_wait = tools_config.delegate_handoff_after_seconds
    if handoff_after_seconds is not None:
        requested_wait = handoff_after_seconds
    if delivery_hint == "background":
        requested_wait = 0.0
    return max(
        0.0, min(float(requested_wait), tools_config.delegate_handoff_max_seconds)
    )


def _delegation_status_poll_seconds(exec_context: ToolExecutionContext) -> float:
    return max(0.05, _tools_config(exec_context).delegate_status_poll_seconds)


def _current_taint_metadata(exec_context: ToolExecutionContext) -> TaintMetadata | None:
    if exec_context.taint_tracker is None:
        return None
    return exec_context.taint_tracker.snapshot().to_metadata()


def _taint_sources_from_metadata(
    metadata: TaintMetadata | None,
) -> tuple[TaintSource, ...]:
    if metadata is None:
        return ()
    return TurnTaintState.from_metadata(metadata).sources


async def _delegation_review_trigger(
    exec_context: ToolExecutionContext,
    *,
    definition: str,
) -> TriggerReviewInput:
    """Build the delegated subconversation's trigger from the delegating turn.

    The goal is composed by this turn's model, so it carries this turn's taint
    and renders as trusted intent only when nothing untrusted entered. The
    human request behind it is propagated separately and judged on its own
    provenance, which is what keeps a delegation off a tainted turn reviewable
    rather than blind.
    """
    return await build_delegation_review_trigger(
        exec_context.db_context,
        trigger_type="delegation_request",
        active_request_role="user",
        definition=definition,
        definition_taint_metadata=_current_taint_metadata(exec_context),
        payload_present=False,
        source_turn_id=exec_context.turn_id,
        source_messages=exec_context.tool_call_review_messages,
        inherited=exec_context.tool_call_review_trigger,
    )


def _terminal_delegation_run(run: DelegationRunDict | None) -> bool:
    return run is not None and run["status"] in TERMINAL_DELEGATION_STATUSES


async def _load_delegation_run(
    exec_context: ToolExecutionContext, delegation_id: str
) -> DelegationRunDict | None:
    return await exec_context.db_context.delegation_runs.get_by_delegation_id(
        delegation_id
    )


def _resume_already_in_progress_result(resume_delegation_id: str) -> ToolResult:
    """Error returned when another resume of the same delegation is still running."""
    return ToolResult(
        text=(
            f"Error: Cannot resume delegation '{resume_delegation_id}': a resume of "
            "it is already in progress. Wait for that to finish (you will be "
            "notified) before resuming it again."
        ),
        attachments=None,
    )


async def _resolve_resume_subconversation(
    exec_context: ToolExecutionContext,
    *,
    resume_delegation_id: str,
    source_service_id: str,
    target_service_id: str,
) -> tuple[str | None, ToolResult | None]:
    """Resolve a resume reference to the subconversation history to continue.

    Returns ``(subconversation_id, None)`` when the prior run can be resumed, or
    ``(None, error_result)`` describing why it cannot. A resumable run must have
    been created by this exact caller — same conversation, interface, user, source
    profile, and source subconversation — and target the same profile, and have
    reached a terminal state. Rationale for each dimension:

    - user: in a shared conversation, resuming another participant's delegation
      would replay their private, account-scoped history under a different caller.
    - source profile: a different, possibly more privileged, profile (e.g. the
      confirm-gated engineer) may have seeded the delegation with context the
      current profile cannot read.
    - source subconversation: one profile can hold several isolated delegated
      histories; matching the caller's ``subconversation_id`` keeps a resume tied
      to the same parent task, so an unrelated sibling task cannot pull in its
      history.
    - target profile: delegated history is scoped by both subconversation and
      profile, so a cross-profile resume would silently load nothing.
    - terminal: an in-flight run cannot be appended to.

    The lookup uses the live database context (as ``get_delegation_status`` does)
    so a prior run committed by an earlier turn is visible. A run that is not the
    caller's own is reported as not found rather than as a permission error, so its
    existence is not disclosed to a caller that may not be entitled to know about
    it.
    """
    prior_run = await exec_context.db_context.delegation_runs.get_by_delegation_id(
        resume_delegation_id
    )
    if prior_run is None or (
        prior_run["conversation_id"] != exec_context.conversation_id
        or prior_run["interface_type"] != exec_context.interface_type
        or prior_run["user_id"] != exec_context.user_id
        or prior_run["source_profile_id"] != source_service_id
        or prior_run["source_subconversation_id"] != exec_context.subconversation_id
    ):
        return None, ToolResult(
            text=(
                f"Error: Cannot resume delegation '{resume_delegation_id}': no such "
                "delegation reference in this conversation. Use list_delegations to "
                "find a valid reference."
            ),
            attachments=None,
        )
    if prior_run["target_service_id"] != target_service_id:
        return None, ToolResult(
            text=(
                f"Error: Cannot resume delegation '{resume_delegation_id}': it was "
                f"delegated to '{prior_run['target_service_id']}', not "
                f"'{target_service_id}'. Resume with the same target_service_id."
            ),
            attachments=None,
        )
    if prior_run["status"] not in TERMINAL_DELEGATION_STATUSES:
        return None, ToolResult(
            text=(
                f"Error: Cannot resume delegation '{resume_delegation_id}': it is "
                f"still {prior_run['status']}. Wait for it to finish (you will be "
                "notified) before resuming it."
            ),
            attachments=None,
        )
    resumed_subconversation_id = prior_run["subconversation_id"]
    # Serialize resumes: two runs sharing a subconversation would interleave
    # messages and tool side effects in the same delegated history. This preflight
    # gives a clean early error for the common sequential case; the unique
    # active-subconversation index is the atomic backstop that closes the
    # check-then-insert race (see the run-creation handling below).
    if await exec_context.db_context.delegation_runs.has_active_run_for_subconversation(
        conversation_id=exec_context.conversation_id,
        subconversation_id=resumed_subconversation_id,
    ):
        return None, _resume_already_in_progress_result(resume_delegation_id)
    return resumed_subconversation_id, None


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

    try:
        metadata_by_id = await exec_context.attachment_registry.get_attachments(
            exec_context.db_context,
            response_attachment_ids,
            acting_user_id=exec_context.user_id,
        )
    except Exception:
        logger.exception(
            "Error fetching delegated attachment metadata for %s",
            response_attachment_ids,
        )
        metadata_by_id = {}

    delegated_attachments: list[ToolAttachment] = []
    for attachment_id in response_attachment_ids:
        att_metadata = metadata_by_id.get(attachment_id)
        if att_metadata is None:
            logger.warning(
                "Could not fetch metadata for delegated attachment %s",
                attachment_id,
            )
        delegated_attachments.append(
            ToolAttachment(
                mime_type=att_metadata.mime_type
                if att_metadata
                else "application/octet-stream",
                content=None,
                attachment_id=attachment_id,
                description=(att_metadata.description if att_metadata else None)
                or f"Attachment from delegated service '{target_service_id}'",
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

    delegated_attachments = await _delegated_attachment_refs(
        exec_context,
        target_service_id=target_service_id,
        response_attachment_ids=response_attachment_ids,
    )

    if not final_text_reply:
        logger.info(
            "Delegated service '%s' returned no textual reply.",
            target_service_id,
        )
        return ToolResult(
            text=f"Service '{target_service_id}' processed the request but provided no textual response.",
            attachments=delegated_attachments,
        )

    return ToolResult(text=final_text_reply, attachments=delegated_attachments)


async def _synchronous_delegation_result(
    exec_context: ToolExecutionContext,
    *,
    target_service: Any,  # noqa: ANN401 - target is a registry-resolved processing service
    target_service_id: str,
    content_parts: list[ContentPartDict],
) -> ToolResult:
    """Run a delegated request inline and return its result as a tool result.

    This is the pre-async (synchronous) delegation path, kept behind the
    ``async_delegation_enabled`` flag so async profile delegation can be disabled
    at runtime: the target profile runs in-process within this tool call, with no
    durable delegation run, worker handoff, or completion notification.

    This path always mints a fresh subconversation. Resuming a prior delegation is
    rejected before reaching here (see ``delegate_to_service_tool``) because,
    without a durable run row, it cannot atomically claim the resumed history
    against concurrent runs via the active-subconversation unique index.
    """
    subconversation_id = str(uuid.uuid4())
    logger.info(
        "Delegating request to service profile '%s' synchronously with %d content "
        "parts (subconversation_id=%s)",
        target_service_id,
        len(content_parts),
        subconversation_id,
    )
    try:
        result = await target_service.handle_chat_interaction(
            db_context=exec_context.db_context,
            interface_type=exec_context.interface_type,
            conversation_id=exec_context.conversation_id,
            trigger_content_parts=content_parts,
            trigger_interface_message_id=None,
            user_name=exec_context.user_name,
            replied_to_interface_id=None,
            chat_interface=exec_context.chat_interface,
            chat_interfaces=exec_context.chat_interfaces,
            confirmation_ui_managers=exec_context.confirmation_ui_managers,
            request_confirmation_callback=exec_context.request_confirmation_callback,
            subconversation_id=subconversation_id,
            initial_taint_sources=_taint_sources_from_metadata(
                _current_taint_metadata(exec_context)
            ),
            tool_call_review_trigger=await _delegation_review_trigger(
                exec_context,
                definition=json.dumps(content_parts, sort_keys=True),
            ),
        )
    except Exception as e:
        logger.exception(
            f"Failed to delegate request to service '{target_service_id}': {e}"
        )
        return ToolResult(
            text=f"Error: Failed to delegate task to service '{target_service_id}'. Details: {e}",
            attachments=None,
        )

    if result.error_traceback:
        logger.error(
            "Delegated service '%s' returned an error: %s",
            target_service_id,
            result.error_traceback,
        )
        detail = (
            short_error_summary(result.error_traceback)
            or "An error occurred during processing."
        )
        return ToolResult(
            text=f"Error from '{target_service_id}' service: {detail}",
            attachments=None,
        )

    final_text_reply = result.text_reply
    delegated_attachments = await _delegated_attachment_refs(
        exec_context,
        target_service_id=target_service_id,
        response_attachment_ids=result.attachment_ids or [],
    )
    if not final_text_reply:
        logger.info(
            "Delegated service '%s' returned no textual reply.",
            target_service_id,
        )
        return ToolResult(
            text=f"Service '{target_service_id}' processed the request but provided no textual response.",
            attachments=delegated_attachments,
        )
    return ToolResult(text=final_text_reply, attachments=delegated_attachments)


def short_error_summary(error: str | None) -> str | None:
    """Return the last non-blank line of an error, or ``None`` if there is none.

    Delegated-run errors may be full tracebacks or whitespace-only strings; this
    guards against ``IndexError`` from ``splitlines()[-1]`` on blank input.
    """
    if not error:
        return None
    lines = [line for line in error.strip().splitlines() if line.strip()]
    return lines[-1] if lines else None


def _failed_delegation_result(
    *, target_service_id: str, run: DelegationRunDict
) -> ToolResult:
    """Surface a concise failure detail and how to retrieve the full status."""
    detail = short_error_summary(run["error"]) or "An error occurred during processing."
    return ToolResult(
        text=(
            f"Error from '{target_service_id}' service: {detail}\n"
            f"Reference: {run['delegation_id']} "
            "(call get_delegation_status with this reference for full details)."
        ),
        attachments=None,
        data={
            "delegation_id": run["delegation_id"],
            "target_service_id": target_service_id,
            "status": "failed",
            "error": detail,
        },
    )


async def _mark_delegation_delivered_inline(
    exec_context: ToolExecutionContext, *, delegation_id: str
) -> None:
    """Record that a terminal run was delivered to the caller inline.

    The inline result is returned to the model as the tool output rather than
    posted to the conversation, so ``handed_off_at`` stays NULL and the worker's
    handed-off-gated notification is skipped. Marking ``notified_at`` here stops
    the cleanup sweep's ``find_terminal_unnotified`` backstop from re-delivering
    the same result into the conversation once the run ages past its window.
    """
    await exec_context.db_context.delegation_runs.mark_notified(
        delegation_id=delegation_id,
        result_message_internal_id=None,
        notified_at=_now(exec_context),
    )


async def _inline_delegation_result(
    exec_context: ToolExecutionContext,
    *,
    target_service_id: str,
    run: DelegationRunDict | None,
) -> ToolResult | None:
    """Return an inline tool result if the run is terminal, else ``None``."""
    if run is None:
        return None
    if run["status"] == "completed":
        logger.info(
            "Delegated service '%s' completed inline for %s.",
            target_service_id,
            run["delegation_id"],
        )
        result = await _completed_delegation_result(
            exec_context,
            target_service_id=target_service_id,
            run=run,
        )
    elif run["status"] == "failed":
        logger.error(
            "Delegated service '%s' failed inline for %s: %s",
            target_service_id,
            run["delegation_id"],
            run["error"],
        )
        result = _failed_delegation_result(target_service_id=target_service_id, run=run)
    else:
        return None

    await _mark_delegation_delivered_inline(
        exec_context, delegation_id=run["delegation_id"]
    )
    return result


def _delegation_reference_text(
    *, delegation_id: str, target_service_id: str, status: str
) -> str:
    return (
        "Delegation handed off and is now running in the background.\n"
        f"Reference: {delegation_id}\n"
        f"Target profile: {target_service_id}\n"
        f"Status: {status}\n"
        "The result will wake this profile automatically when it finishes, and "
        "your follow-up response will be delivered to the conversation. You do "
        "NOT need to check on it. Let the user know the work is in progress, "
        "then end your turn. Do not call get_delegation_status in a loop to wait "
        "for it; only look it up if the user later asks for an update or you need "
        "the full error detail for a failed delegation."
    )


_PENDING_DELEGATION_NUDGE = (
    "This delegation is still running. You will be notified automatically in this "
    "conversation when it finishes, so do not poll in a loop — end your turn instead."
)


def _has_pending_delegation(summaries: Iterable[DelegationRunSummary]) -> bool:
    return any(
        summary["status"] not in TERMINAL_DELEGATION_STATUSES for summary in summaries
    )


def _format_delegation_summary(summary: DelegationRunSummary) -> str:
    return json.dumps(summary, indent=2, default=str)


@dataclass(frozen=True)
class _QueuedDelegation:
    delegation_id: str
    target_service_id: str
    wait_seconds: float


def _resolve_delegation_target(
    exec_context: ToolExecutionContext,
    target_service_id: str,
) -> tuple[DelegatableService | None, str | None, ToolResult | None]:
    """Resolve the target and enforce its source-profile delegation boundary."""
    processing_service = exec_context.processing_service
    if not processing_service or not processing_service.processing_services_registry:
        logger.error(
            "Processing services registry not available in the current execution context."
        )
        return (
            None,
            None,
            ToolResult(
                text="Error: Service registry is not available to delegate the task.",
                attachments=None,
            ),
        )

    target_service = processing_service.processing_services_registry.get(
        target_service_id
    )
    if not target_service:
        logger.error(
            "Target service profile ID '%s' not found in the registry.",
            target_service_id,
        )
        return (
            None,
            None,
            ToolResult(
                text=f"Error: Target service profile '{target_service_id}' not found.",
                attachments=None,
            ),
        )

    source_service_id = processing_service.service_config.id
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
        return (
            None,
            None,
            ToolResult(
                text=(
                    "Error: Tool 'delegate_to_service' is not allowed. "
                    f"Profile '{source_service_id}' is not permitted to delegate "
                    f"to '{target_service_id}'."
                ),
                attachments=None,
            ),
        )
    return target_service, source_service_id, None


async def _resolve_requested_subconversation(
    exec_context: ToolExecutionContext,
    *,
    resume_delegation_id: str | None,
    source_service_id: str,
    target_service_id: str,
) -> tuple[str | None, str | None, ToolResult | None]:
    """Normalize a resume reference and validate the history it selects."""
    normalized_delegation_id = (resume_delegation_id or "").strip() or None
    if normalized_delegation_id is None:
        return None, None, None
    subconversation_id, error = await _resolve_resume_subconversation(
        exec_context,
        resume_delegation_id=normalized_delegation_id,
        source_service_id=source_service_id,
        target_service_id=target_service_id,
    )
    return normalized_delegation_id, subconversation_id, error


def _durable_authorization_matches(
    durable_authorization: ToolConfirmationAuthorization | None,
    effective_arguments: dict[str, object],
) -> bool:
    if durable_authorization is None:
        return False
    if durable_authorization.tool_name != "delegate_to_service":
        return False
    if not {"target_service_id", "user_request"}.issubset(
        durable_authorization.tool_args
    ):
        return False

    for key, value in durable_authorization.tool_args.items():
        if key not in effective_arguments:
            return False
        normalized_value = value
        if key == "resume_delegation_id" and isinstance(value, str):
            normalized_value = value.strip() or None
        if normalized_value != effective_arguments[key]:
            return False
    return True


def _confirmation_tool_arguments(
    *,
    target_service_id: str,
    user_request: str,
    confirm_delegation: bool,
    attachment_ids: list[str] | None,
    resume_delegation_id: str | None,
) -> ToolArguments:
    arguments: ToolArguments = {
        "target_service_id": target_service_id,
        "user_request": user_request,
        "confirm_delegation": confirm_delegation,
    }
    if attachment_ids is not None:
        arguments["attachment_ids"] = attachment_ids
    if resume_delegation_id is not None:
        arguments["resume_delegation_id"] = resume_delegation_id
    return arguments


async def _request_delegation_confirmation(
    exec_context: ToolExecutionContext,
    *,
    target_service_id: str,
    call_id: str,
    tool_args: ToolArguments,
) -> ConfirmationOutcome | ToolResult:
    callback = exec_context.request_confirmation_callback
    assert callback is not None
    try:
        return await callback(
            interface_type=exec_context.interface_type,
            conversation_id=exec_context.conversation_id,
            turn_id=exec_context.turn_id,
            tool_name="delegate_to_service",
            call_id=call_id,
            tool_args=tool_args,
            timeout_seconds=_tools_config(exec_context).confirmation_timeout_seconds,
            context=exec_context,
        )
    except TimeoutError:
        logger.warning(
            "Confirmation for delegating to '%s' timed out.", target_service_id
        )
        return ToolResult(
            text=f"Error: Confirmation timed out for delegating to '{target_service_id}'.",
            attachments=None,
        )
    except Exception as error:
        logger.exception(
            "Error during confirmation for delegating to '%s': %s",
            target_service_id,
            error,
        )
        return ToolResult(
            text=f"Error during confirmation for delegating to '{target_service_id}': {error}",
            attachments=None,
        )


async def _confirm_delegation_if_required(
    exec_context: ToolExecutionContext,
    *,
    target_service_id: str,
    user_request: str,
    confirm_delegation: bool,
    attachment_ids: list[str] | None,
    handoff_after_seconds: float | None,
    delivery_hint: Literal["auto", "background"],
    resume_delegation_id: str | None,
) -> ToolResult | None:
    """Apply delegation-specific confirmation limits and durable authorization."""
    if not confirm_delegation:
        return None

    over_length_reason = over_length_delegation_block_reason(user_request)
    if over_length_reason is not None:
        logger.warning(
            "Refusing confirm-gated delegation to '%s': request is %d chars (limit %d).",
            target_service_id,
            len(user_request),
            MAX_DELEGATION_REQUEST_CHARS,
        )
        return ToolResult(text=over_length_reason, attachments=None)

    confirmation_tool_args = _confirmation_tool_arguments(
        target_service_id=target_service_id,
        user_request=user_request,
        confirm_delegation=confirm_delegation,
        attachment_ids=attachment_ids,
        resume_delegation_id=resume_delegation_id,
    )
    durable_authorization = exec_context.tool_confirmation_authorization
    durable_authorization_matches = _durable_authorization_matches(
        durable_authorization,
        {
            "target_service_id": target_service_id,
            "user_request": user_request,
            "confirm_delegation": confirm_delegation,
            "attachment_ids": attachment_ids,
            "handoff_after_seconds": handoff_after_seconds,
            "delivery_hint": delivery_hint,
            "resume_delegation_id": resume_delegation_id,
        },
    )
    matched_authorization = (
        durable_authorization if durable_authorization_matches else None
    )
    if matched_authorization is not None:
        confirmation_tool_args = dict(matched_authorization.tool_args)
        if matched_authorization.consumed:
            logger.info(
                "Durable approval already satisfied confirmation for exact "
                "delegate_to_service call %s",
                matched_authorization.call_id,
            )
            return None

    callback = exec_context.request_confirmation_callback
    if not callback:
        logger.error(
            "Confirmation required for delegating to '%s', but no confirmation "
            "callback is available. Aborting delegation.",
            target_service_id,
        )
        return ToolResult(
            text=f"Error: Confirmation required to delegate to '{target_service_id}', but no confirmation mechanism is available.",
            attachments=None,
        )

    call_id = (
        matched_authorization.call_id
        if matched_authorization is not None
        else f"delegate_to_service_{uuid.uuid4()}"
    )
    confirmation_outcome = await _request_delegation_confirmation(
        exec_context,
        target_service_id=target_service_id,
        call_id=call_id,
        tool_args=confirmation_tool_args,
    )
    if isinstance(confirmation_outcome, ToolResult):
        return confirmation_outcome
    if confirmation_outcome.kind == "approved":
        return None
    return _delegation_confirmation_outcome_result(
        target_service_id,
        confirmation_outcome,
    )


async def _delegation_content_parts(
    exec_context: ToolExecutionContext,
    *,
    target_service_id: str,
    user_request: str,
    attachment_ids: list[str] | None,
) -> tuple[list[ContentPartDict], ToolResult | None]:
    """Build delegated content after validating attachment ownership."""
    content_parts: list[ContentPartDict] = [text_content(user_request)]
    if not attachment_ids:
        return content_parts, None
    if not exec_context.attachment_registry:
        logger.warning(
            "Attachment IDs provided but AttachmentRegistry not available - ignoring attachments"
        )
        return content_parts, None

    found = await exec_context.attachment_registry.get_attachments(
        exec_context.db_context,
        attachment_ids,
        acting_user_id=exec_context.user_id,
    )
    missing = [
        attachment_id for attachment_id in attachment_ids if attachment_id not in found
    ]
    if missing:
        return content_parts, ToolResult(
            text=(
                f"Error: Cannot delegate to '{target_service_id}': "
                f"attachment(s) {', '.join(missing)} do not exist or "
                "belong to another user."
            ),
            attachments=None,
        )
    content_parts.extend(
        attachment_content(attachment_id) for attachment_id in attachment_ids
    )
    return content_parts, None


async def _synchronous_result_if_required(
    exec_context: ToolExecutionContext,
    *,
    target_service: DelegatableService,
    target_service_id: str,
    content_parts: list[ContentPartDict],
    resume_delegation_id: str | None,
    resumed_subconversation_id: str | None,
) -> ToolResult | None:
    """Run inline when async delivery cannot be used, rejecting unsafe resumes."""
    if not (
        exec_context.in_script
        or not _tools_config(exec_context).async_delegation_enabled
    ):
        return None
    if resumed_subconversation_id is not None:
        return ToolResult(
            text=(
                f"Error: Cannot resume delegation '{resume_delegation_id}' here: "
                "resuming is only supported for asynchronous delegations. This "
                "call runs synchronously (inside a script, or async delegation is "
                "disabled). Start a fresh delegation instead (omit "
                "resume_delegation_id)."
            ),
            attachments=None,
        )
    return await _synchronous_delegation_result(
        exec_context,
        target_service=target_service,
        target_service_id=target_service_id,
        content_parts=content_parts,
    )


async def _enqueue_delegation(
    exec_context: ToolExecutionContext,
    *,
    source_service_id: str,
    target_service_id: str,
    user_request: str,
    content_parts: list[ContentPartDict],
    handoff_after_seconds: float | None,
    delivery_hint: Literal["auto", "background"],
    resume_delegation_id: str | None,
    resumed_subconversation_id: str | None,
) -> _QueuedDelegation | ToolResult:
    """Atomically persist a delegated run and its task, including resume claims."""
    delegation_id = f"delegation_{uuid.uuid4().hex}"
    task_id = f"{DELEGATED_PROFILE_RUN_TASK_TYPE}_{uuid.uuid4().hex}"
    subconversation_id = resumed_subconversation_id or str(uuid.uuid4())
    wait_seconds = _resolve_handoff_wait_seconds(
        exec_context,
        handoff_after_seconds,
        delivery_hint,
    )
    taint_state_json = _current_taint_metadata(exec_context)

    logger.info(
        "Enqueuing delegated request to service profile '%s' with %d content parts "
        "(delegation_id=%s, subconversation_id=%s, wait_seconds=%.2f)",
        target_service_id,
        len(content_parts),
        delegation_id,
        subconversation_id,
        wait_seconds,
    )

    async def _enqueue_delegated_run(txn: DatabaseTransaction) -> None:
        await txn.delegation_runs.create_run({
            "delegation_id": delegation_id,
            "task_id": task_id,
            "source_profile_id": source_service_id,
            "target_service_id": target_service_id,
            "interface_type": exec_context.interface_type,
            "conversation_id": exec_context.conversation_id,
            "user_id": exec_context.user_id,
            "user_name": exec_context.user_name,
            "source_turn_id": exec_context.turn_id,
            "source_subconversation_id": exec_context.subconversation_id,
            "subconversation_id": subconversation_id,
            "request_text": user_request,
            "content_parts_json": content_parts,
            "taint_state_json": taint_state_json,
        })
        await txn.tasks.enqueue(
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

    try:
        await exec_context.db_context.atomic(_enqueue_delegated_run)
    except IntegrityError:
        if resumed_subconversation_id is not None:
            logger.info(
                "Concurrent resume of delegation %s rejected by the unique "
                "active-subconversation constraint (subconversation=%s).",
                resume_delegation_id,
                subconversation_id,
            )
            return _resume_already_in_progress_result(cast("str", resume_delegation_id))
        logger.exception(
            "Failed to delegate request to service '%s' due to a constraint violation.",
            target_service_id,
        )
        return ToolResult(
            text=f"Error: Failed to delegate task to service '{target_service_id}'.",
            attachments=None,
        )
    except Exception as error:
        logger.exception(
            "Failed to delegate request to service '%s': %s",
            target_service_id,
            error,
        )
        return ToolResult(
            text=f"Error: Failed to delegate task to service '{target_service_id}'. Details: {error}",
            attachments=None,
        )
    return _QueuedDelegation(
        delegation_id=delegation_id,
        target_service_id=target_service_id,
        wait_seconds=wait_seconds,
    )


async def _await_or_handoff_delegation(
    exec_context: ToolExecutionContext,
    queued: _QueuedDelegation,
) -> ToolResult:
    """Race-safely return a fast result or hand its delivery to the worker."""
    delegation_id = queued.delegation_id

    async def await_inline_result() -> ToolResult | str:
        run = await _wait_for_delegation_run(
            exec_context,
            delegation_id=delegation_id,
            wait_seconds=queued.wait_seconds,
        )
        inline_result = await _inline_delegation_result(
            exec_context,
            target_service_id=queued.target_service_id,
            run=run,
        )
        if inline_result is not None:
            return inline_result

        handed_off = await exec_context.db_context.delegation_runs.mark_handed_off(
            delegation_id,
            _now(exec_context),
        )
        if not handed_off:
            run = await _load_delegation_run(exec_context, delegation_id)
            inline_result = await _inline_delegation_result(
                exec_context,
                target_service_id=queued.target_service_id,
                run=run,
            )
            if inline_result is not None:
                return inline_result
        return run["status"] if run is not None else "queued"

    try:
        inline_outcome = await await_inline_result()
        if isinstance(inline_outcome, ToolResult):
            return inline_outcome
        run_status = inline_outcome
    except Exception:
        logger.exception(
            "Error awaiting inline result for delegation %s; returning async "
            "reference. Claiming the handoff so the worker delivers the result.",
            delegation_id,
        )
        try:
            await exec_context.db_context.delegation_runs.mark_handed_off(
                delegation_id,
                _now(exec_context),
            )
        except Exception:
            logger.exception(
                "Failed to claim handoff for delegation %s after wait error; the "
                "cleanup sweep is the backstop.",
                delegation_id,
            )
        run_status = "running"

    return ToolResult(
        text=_delegation_reference_text(
            delegation_id=delegation_id,
            target_service_id=queued.target_service_id,
            status=run_status,
        ),
        attachments=None,
        data={
            "delegation_id": delegation_id,
            "target_service_id": queued.target_service_id,
            "status": run_status,
        },
    )


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
                "for the task. The available service profiles and their descriptions are listed in "
                "your system prompt. Profile-to-profile delegation controls are enforced by the "
                "tool policy engine.\n\n"
                "Returns the delegated service's text response, or — when the work runs long — an "
                "async reference ID. When you get a reference, the result wakes this profile "
                "automatically once it finishes and your follow-up is delivered to the conversation, "
                "so end your turn instead of polling get_delegation_status in a loop. Errors are "
                "returned as text (and, for failed async runs, a reference ID you can pass to "
                "get_delegation_status for the full detail).\n\n"
                "Each delegation starts a fresh, isolated conversation with the target profile. To "
                "instead continue a previous finished delegation — so the target profile keeps the "
                "context of that earlier exchange — pass its reference ID as resume_delegation_id "
                "(with the same target_service_id). Use list_delegations to find the reference."
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
                        "description": "Optional list of attachment UUIDs to include with the delegated request. These attachments must be accessible in the current conversation and are passed to the target service for processing -- including a service running on a remote agent, which receives the file itself rather than a reference. Files the service produces come back attached to its result, so you can pass them on or work with them by id.",
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
                    "resume_delegation_id": {
                        "type": "string",
                        "description": "Optional. Reference ID of a previous finished delegation (from delegate_to_service or list_delegations) to continue instead of starting a fresh conversation. The target profile resumes that delegation's history and retains its context. Must reference a completed or failed delegation to the same target_service_id in this conversation.",
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
                        "description": "Optional status filter such as queued, running, completed, or failed.",
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
    resume_delegation_id: str | None = None,
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
        resume_delegation_id: Optional reference to a prior finished delegation to
            the same target profile. When set, the delegated profile continues that
            delegation's isolated history instead of starting fresh, so it retains
            the earlier exchange's context.

    Returns:
        ToolResult with response text from the target service and any attachments it generated
    """
    logger.info(
        f"Executing delegate_to_service_tool: target='{target_service_id}', request='{user_request[:50]}...', confirm={confirm_delegation}"
    )

    target_service, source_service_id, target_error = _resolve_delegation_target(
        exec_context,
        target_service_id,
    )
    if target_error is not None:
        return target_error
    target_service = cast("DelegatableService", target_service)
    source_service_id = cast("str", source_service_id)

    (
        resume_delegation_id,
        resumed_subconversation_id,
        resume_error,
    ) = await _resolve_requested_subconversation(
        exec_context,
        resume_delegation_id=resume_delegation_id,
        source_service_id=source_service_id,
        target_service_id=target_service_id,
    )
    if resume_error is not None:
        return resume_error

    confirmation_error = await _confirm_delegation_if_required(
        exec_context,
        target_service_id=target_service_id,
        user_request=user_request,
        confirm_delegation=confirm_delegation,
        attachment_ids=attachment_ids,
        handoff_after_seconds=handoff_after_seconds,
        delivery_hint=delivery_hint,
        resume_delegation_id=resume_delegation_id,
    )
    if confirmation_error is not None:
        return confirmation_error

    content_parts, attachment_error = await _delegation_content_parts(
        exec_context,
        target_service_id=target_service_id,
        user_request=user_request,
        attachment_ids=attachment_ids,
    )
    if attachment_error is not None:
        return attachment_error

    synchronous_result = await _synchronous_result_if_required(
        exec_context,
        target_service=target_service,
        target_service_id=target_service_id,
        content_parts=content_parts,
        resume_delegation_id=resume_delegation_id,
        resumed_subconversation_id=resumed_subconversation_id,
    )
    if synchronous_result is not None:
        return synchronous_result

    if delivery_hint not in {"auto", "background"}:
        return ToolResult(
            text="Error: delivery_hint must be 'auto' or 'background'.",
            attachments=None,
        )

    enqueue_result = await _enqueue_delegation(
        exec_context,
        source_service_id=source_service_id,
        target_service_id=target_service_id,
        user_request=user_request,
        content_parts=content_parts,
        handoff_after_seconds=handoff_after_seconds,
        delivery_hint=delivery_hint,
        resume_delegation_id=resume_delegation_id,
        resumed_subconversation_id=resumed_subconversation_id,
    )
    if isinstance(enqueue_result, ToolResult):
        return enqueue_result

    return await _await_or_handoff_delegation(exec_context, enqueue_result)


async def get_delegation_status_tool(
    exec_context: ToolExecutionContext,
    delegation_id: str,
) -> ToolResult:
    """Return status for an asynchronous profile delegation."""
    if not _tools_config(exec_context).async_delegation_enabled:
        return ToolResult(
            text=(
                "Async profile delegation is disabled; delegations run synchronously "
                "and return their result directly, so there are no delegation "
                "references to look up."
            ),
            attachments=None,
        )
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
    text = _format_delegation_summary(summary)
    if _has_pending_delegation([summary]):
        text = f"{text}\n\n{_PENDING_DELEGATION_NUDGE}"
    return ToolResult(
        text=text,
        data=cast("dict[str, Any]", summary),
    )


async def list_delegations_tool(
    exec_context: ToolExecutionContext,
    status: str | None = None,
    limit: int = 10,
) -> ToolResult:
    """List recent asynchronous profile delegations for the current conversation."""
    if not _tools_config(exec_context).async_delegation_enabled:
        return ToolResult(
            text=(
                "Async profile delegation is disabled; delegations run synchronously "
                "and leave no delegation records to list."
            ),
            data=[],
        )
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
    text = json.dumps(summaries, indent=2, default=str)
    if _has_pending_delegation(summaries):
        text = f"{text}\n\n{_PENDING_DELEGATION_NUDGE}"
    return ToolResult(
        text=text,
        data=summaries,
    )
