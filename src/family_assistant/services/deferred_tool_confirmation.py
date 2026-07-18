"""Defer confirm-gated tool calls to durable confirmations.

Non-interactive task-worker paths (inbound email actions, automation scripts)
cannot prompt the user inline when a tool requires confirmation. Instead they
record a durable confirmation request, deliver it to the user's primary channel,
and return immediately with a "pending approval" result. The tool runs later via
the ``confirmation_tool_execution`` task once the user approves.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from family_assistant.services.confirmation_service import (
    ConfirmationService,
    build_confirmation_policy_fingerprint,
    create_durable_confirmation,
)
from family_assistant.services.user_identity import UserIdentityResolver
from family_assistant.storage.context import get_db_context
from family_assistant.tools.confirmation import TOOL_CONFIRMATION_RENDERERS
from family_assistant.tools.types import ConfirmationOutcome

if TYPE_CHECKING:
    from collections.abc import Callable

    from family_assistant.tools.types import (
        RequestConfirmationCallback,
        ToolArguments,
        ToolArgumentsView,
        ToolExecutionContext,
    )

logger = logging.getLogger(__name__)

MAX_CONFIRMATION_ARGS_CHARS = 6000


def _markdown_code_block(text: str, *, language: str = "") -> str:
    """Render a markdown code block with a fence longer than any content fence."""
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}{language}\n{text}\n{fence}"


async def render_tool_confirmation_prompt(
    *,
    tool_name: str,
    tool_args: ToolArgumentsView,
    context: ToolExecutionContext,
    source_prefix: str,
) -> str:
    """Render a human-facing confirmation prompt for a deferred tool call."""
    renderer = TOOL_CONFIRMATION_RENDERERS.get(tool_name)
    if renderer is not None:
        rendered = await renderer(tool_args, context)
    else:
        args_json = json.dumps(tool_args, indent=2, sort_keys=True, default=str)
        if len(args_json) > MAX_CONFIRMATION_ARGS_CHARS:
            args_json = args_json[:MAX_CONFIRMATION_ARGS_CHARS] + "\n... [truncated]"
        rendered = (
            f"Tool: {tool_name}\n\n"
            "Arguments:\n"
            f"{_markdown_code_block(args_json, language='json')}"
        )
    return f"{source_prefix}\n\n{rendered}"


async def deliver_confirmation_to_primary_channel(
    *,
    context: ToolExecutionContext,
    target_user_id: str,
    request_id: str,
    confirmation_prompt: str,
) -> str | None:
    """Send the Telegram confirmation UI for a durable request.

    Returns a human-readable warning string if delivery could not be performed
    (the durable request still exists and can be approved elsewhere), or None on
    success.
    """
    if context.confirmation_ui_managers is None:
        message = "No confirmation UI manager registry is available."
        logger.info("%s Deferred confirmation: %s", message, request_id)
        return message
    telegram_confirmation_manager = context.confirmation_ui_managers.get("telegram")
    if telegram_confirmation_manager is None:
        message = "No Telegram confirmation UI is available."
        logger.info("%s Deferred confirmation: %s", message, request_id)
        return message

    processing_service = context.processing_service
    if processing_service is None:
        message = (
            "No processing service is available to resolve the user's Telegram "
            "notification target."
        )
        logger.info("%s Deferred confirmation: %s", message, request_id)
        return message
    telegram_user_id = UserIdentityResolver(
        processing_service.app_config
    ).get_primary_telegram_user_id(target_user_id)
    if telegram_user_id is None:
        message = (
            f"User {target_user_id!r} has no primary Telegram mapping for "
            "confirmation delivery."
        )
        logger.info("%s Deferred confirmation: %s", message, request_id)
        return message

    outcome = await telegram_confirmation_manager.send_existing_confirmation_request(
        conversation_id=str(telegram_user_id),
        request_id=request_id,
        prompt_text=confirmation_prompt,
    )
    if outcome.kind != "completed":
        message = (
            f"Could not send Telegram confirmation UI to user {telegram_user_id}: "
            f"{outcome.result or outcome.kind}."
        )
        logger.warning("%s Deferred confirmation: %s", message, request_id)
        return message
    logger.info(
        "Sent Telegram confirmation UI to user %s for deferred confirmation %s",
        telegram_user_id,
        request_id,
    )
    return None


async def create_deferred_tool_confirmation(
    *,
    context: ToolExecutionContext,
    tool_name: str,
    call_id: str,
    tool_args: ToolArguments,
    timeout_seconds: float,
    target_user_id: str,
    source_prefix: str,
    link_source_message: bool = True,
) -> ConfirmationOutcome:
    """Record a durable confirmation for a confirm-gated tool and notify the user.

    Returns a ``completed`` outcome whose ``result`` tells the caller the tool has
    not run yet and is awaiting approval. The policy layer surfaces this result in
    place of the tool's return value.

    ``link_source_message`` resolves the originating user message from
    ``context.turn_id`` so an approval can thread back to it. Callers whose turn's
    source message lives in an uncommitted, ambient transaction (e.g. the
    delegation-completion wakeup, whose data message is written in an isolated
    context held open across the turn) must pass ``False``: the durable
    confirmation is written by the confirmation service's own short transaction,
    which cannot see that row and would violate the foreign key.
    """
    confirmation_prompt = await render_tool_confirmation_prompt(
        tool_name=tool_name,
        tool_args=tool_args,
        context=context,
        source_prefix=source_prefix,
    )
    confirmation_service = ConfirmationService(
        db_context_factory=lambda: get_db_context(engine=context.db_context.engine)
    )
    now = context.clock.now() if context.clock is not None else datetime.now(UTC)
    taint_state_json = (
        context.taint_tracker.snapshot().to_metadata()
        if context.taint_tracker is not None
        else None
    )
    request = await create_durable_confirmation(
        confirmation_service=confirmation_service,
        db_context=context.db_context,
        target_user_id=target_user_id,
        tool_name=tool_name,
        tool_call_id=call_id,
        tool_args=tool_args,
        confirmation_prompt=confirmation_prompt,
        timeout_seconds=timeout_seconds,
        turn_id=context.turn_id if link_source_message else None,
        now=now,
        processing_profile_id=context.processing_profile_id,
        origin_interface_type=context.interface_type,
        origin_conversation_id=context.conversation_id,
        taint_state_json=taint_state_json,
        approval_policy_fingerprint=build_confirmation_policy_fingerprint(
            tool_name=tool_name,
            tool_call_id=call_id,
            processing_profile_id=context.processing_profile_id,
            taint_state_json=taint_state_json,
        ),
    )
    request_id = str(request["id"])
    logger.info(
        "Created durable confirmation %s for deferred tool %s", request_id, tool_name
    )
    notification_warning = await deliver_confirmation_to_primary_channel(
        context=context,
        target_user_id=target_user_id,
        request_id=request_id,
        confirmation_prompt=confirmation_prompt,
    )
    result = (
        f"Waiting on the user to approve this in Telegram or the web UI "
        f"(request {request_id}). It hasn't run yet."
    )
    if notification_warning is not None:
        result = f"{result}\n\nWarning: {notification_warning}"
    return ConfirmationOutcome(
        kind="completed",
        result=result,
        action_attempted=False,
    )


def build_deferred_confirmation_callback(
    *,
    target_user_id: str | None,
    source_prefix: str,
    missing_owner_message: Callable[[str], str],
) -> RequestConfirmationCallback:
    """Build a confirmation callback that defers confirm-gated calls to durable requests.

    Non-interactive task-worker turns (automation scripts, delegated-task completion
    notifications, scheduled callbacks/reminders) have no live channel to host an
    inline confirmation. This callback records a durable confirmation addressed to
    ``target_user_id`` and returns immediately; the tool runs later via the
    ``confirmation_tool_execution`` task once the user approves. When the owning user
    is unknown the tool cannot be approved and is reported as not run.
    """

    async def _deferred_confirmation_callback(
        interface_type: str,
        conversation_id: str,
        turn_id: str | None,
        tool_name: str,
        call_id: str,
        tool_args: ToolArguments,
        timeout_seconds: float,
        context: ToolExecutionContext,
    ) -> ConfirmationOutcome:
        _ = interface_type
        _ = conversation_id
        _ = turn_id
        if target_user_id is None:
            return ConfirmationOutcome(
                kind="failed",
                result=missing_owner_message(tool_name),
            )
        return await create_deferred_tool_confirmation(
            context=context,
            tool_name=tool_name,
            call_id=call_id,
            tool_args=tool_args,
            timeout_seconds=timeout_seconds,
            target_user_id=target_user_id,
            source_prefix=source_prefix,
            link_source_message=False,
        )

    return _deferred_confirmation_callback
