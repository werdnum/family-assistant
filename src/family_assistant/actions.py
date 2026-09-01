"""
Shared action execution logic for both event listeners and scheduled tasks.
"""

import logging
import time
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from family_assistant.security.definition_records import stamp_callback_definition
from family_assistant.security.taint import TurnTaintTracker
from family_assistant.storage.database import Database
from family_assistant.storage.tasks import enqueue_task

if TYPE_CHECKING:
    from family_assistant.task_worker import LlmCallbackPayload

logger = logging.getLogger(__name__)


class ActionType(StrEnum):
    """Action types supported by the system."""

    WAKE_LLM = "wake_llm"
    SCRIPT = "script"


class WakeLlmProfileError(RuntimeError):
    """Raised when a profile that may not wake the LLM attempts to.

    A woken turn does not necessarily run under the profile that scheduled it.
    Schedule automations, ``schedule_action`` and a script's built-in
    ``wake_llm()`` stamp their originating profile and ``handle_llm_callback``
    resolves it, but event listeners deliberately route to the restricted
    ``event_handler`` profile because the triggering event is untrusted.

    Either way a confined profile must not be able to enqueue a wake: via an
    event listener it would escalate to ``event_handler``, and via a stamped wake
    it would fail at fire time, when ``handle_llm_callback`` re-checks the flag.
    Profiles that must stay confined set ``allow_wake_llm=False`` and are refused
    here, at creation, instead of either.
    """


def assert_wake_llm_allowed(
    action_type: ActionType | str,
    allow_wake_llm: bool,
) -> None:
    """Refuse a wake_llm action from a profile that disallows waking the LLM.

    No-op for scripts and for profiles with ``allow_wake_llm=True`` (the default,
    which preserves existing behavior for the default assistant, event handlers,
    and other full-capability profiles). Raises ``WakeLlmProfileError`` when a
    confined profile (``allow_wake_llm=False``) attempts a wake_llm action.
    """
    if ActionType(action_type) != ActionType.WAKE_LLM:
        return
    if not allow_wake_llm:
        raise WakeLlmProfileError(
            "This profile is not permitted to wake the LLM (allow_wake_llm is "
            "disabled). A woken turn would not stay inside this profile's "
            'confinement. Use action_type="script" and keep results in data '
            "(notes) instead of waking the assistant."
        )


async def execute_action(
    db_ctx: Database,
    action_type: ActionType,
    # ast-grep-ignore: no-dict-any - action config has varying keys per action type
    action_config: dict[str, Any],
    conversation_id: str,
    interface_type: str = "telegram",
    user_name: str | None = None,  # Added user_name
    # ast-grep-ignore: no-dict-any - arbitrary context data passed through to action handlers
    context: dict[str, Any] | None = None,
    scheduled_at: datetime | None = None,
    recurrence_rule: str | None = None,
    processing_profile_id: str | None = None,
    created_by_user_id: str | None = None,
    allow_wake_llm: bool = True,
    tool_call_review_trigger_type: str | None = None,
    tool_call_review_trigger_definition: str | None = None,
    tool_call_review_trigger_payload_present: bool | None = None,
    definition_taint_tracker: TurnTaintTracker | None = None,
) -> None:
    """
    Execute an action. Used by both event listeners and scheduled tasks.

    Args:
        db_ctx: Database context
        action_type: Type of action to execute
        action_config: Configuration for the action
        conversation_id: Conversation to execute in
        interface_type: Interface type (telegram, web, etc)
        user_name: Optional user name context for the action
        context: Additional context (e.g., event data, trigger info)
        scheduled_at: When to execute the action (None for immediate)
        recurrence_rule: RRULE for recurring tasks (None for one-time)
        processing_profile_id: The profile the resulting turn or script runs
            under. Scripts execute under their creating profile so validation and
            execution agree. wake_llm carries it too -- handle_llm_callback
            resolves it fail-loud -- except for event listeners, which the event
            processor stamps with the restricted event_handler profile instead,
            because the triggering event is untrusted.
        created_by_user_id: Creating user for script actions; confirm-gated
            tool calls from the script are addressed to this user.
        definition_taint_tracker: The authoring turn's taint tracker, stamped
            onto the enqueued definition record. Absent means the write cannot
            prove its turn was clean, so the definition stamps unknown_external
            and its firings stay fail-closed.
        allow_wake_llm: Whether the acting profile may wake the LLM. When False,
            a wake_llm action is refused loudly (see assert_wake_llm_allowed)
            rather than being enqueued for a turn that would escape the profile's
            confinement.
    """
    if context is None:
        context = {}

    # A woken turn does not stay inside a confined profile, so such a profile
    # must not be able to enqueue one at all.
    assert_wake_llm_allowed(action_type, allow_wake_llm)

    if action_type == ActionType.WAKE_LLM:
        # Prepare callback context
        callback_context = {
            "trigger": context.get("trigger", "Scheduled action"),
            **context,
        }

        # Include any wake context from config
        if "context" in action_config:
            callback_context["message"] = action_config["context"]

        # The uuid suffix, not just the millisecond stamp: one event can
        # match several listeners, and their enqueues land well inside the
        # same millisecond. task_id is unique, so a bare stamp collides.
        task_id = f"action_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"

        payload: LlmCallbackPayload = {
            "interface_type": interface_type,
            "conversation_id": conversation_id,
            "callback_context": callback_context,
            "scheduling_timestamp": datetime.now(UTC).isoformat(),
            "tool_call_review_trigger_type": (
                tool_call_review_trigger_type or "scheduled_callback"
            ),
            "tool_call_review_trigger_definition": (
                tool_call_review_trigger_definition
                if tool_call_review_trigger_type is not None
                else (
                    str(action_config["context"])
                    if isinstance(action_config.get("context"), str)
                    else None
                )
            ),
            "tool_call_review_trigger_payload_present": (
                tool_call_review_trigger_payload_present
                if tool_call_review_trigger_payload_present is not None
                else False
            ),
        }
        payload["tool_call_review_definition_record"] = stamp_callback_definition(
            payload["tool_call_review_trigger_definition"],
            tracker=definition_taint_tracker,
        )
        if user_name:
            payload["user_name"] = user_name
        if created_by_user_id is not None:
            payload["created_by_user_id"] = created_by_user_id
        # Honor the wake's execution profile (event listeners route to
        # event_handler; one-time schedules carry their originating profile).
        if processing_profile_id is not None:
            payload["processing_profile_id"] = processing_profile_id

        await enqueue_task(
            db_context=db_ctx,
            task_id=task_id,
            task_type="llm_callback",
            payload=payload,
            scheduled_at=scheduled_at,
            recurrence_rule=recurrence_rule,
        )

    elif action_type == ActionType.SCRIPT:
        task_id = f"script_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"

        script_payload: dict[str, object] = {
            "config": action_config,
            "conversation_id": conversation_id,
            "interface_type": interface_type,
            **context,
        }
        if action_config.get("script_code"):
            script_payload["script_code"] = action_config["script_code"]
        elif action_config.get("script_name"):
            script_payload["script_name"] = action_config["script_name"]
            if action_config.get("parameters"):
                script_payload["script_parameters"] = action_config["parameters"]
        if user_name:
            script_payload["user_name"] = user_name
        if processing_profile_id is not None:
            script_payload["processing_profile_id"] = processing_profile_id
        if created_by_user_id is not None:
            script_payload["created_by_user_id"] = created_by_user_id

        await enqueue_task(
            db_context=db_ctx,
            task_id=task_id,
            task_type="script_execution",
            payload=script_payload,
            scheduled_at=scheduled_at,
            recurrence_rule=recurrence_rule,
        )
    else:
        raise ValueError(f"Unknown action type: {action_type}")
