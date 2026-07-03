"""
Shared action execution logic for both event listeners and scheduled tasks.
"""

import logging
import time
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.tasks import enqueue_task

if TYPE_CHECKING:
    from family_assistant.task_worker import LlmCallbackPayload

logger = logging.getLogger(__name__)


class ActionType(StrEnum):
    """Action types supported by the system."""

    WAKE_LLM = "wake_llm"
    SCRIPT = "script"


class WakeLlmProfileError(RuntimeError):
    """Raised when a wake_llm action is stamped with a non-default profile.

    Unlike scripts, wake_llm actions do not honor the automation's stored
    ``processing_profile_id`` at execution time: the llm_callback path never
    receives it and ``handle_llm_callback`` runs under the task worker's default
    trusted profile. A wake_llm automation created under a confined profile would
    therefore silently execute with full tools and no label confinement. Rather
    than downgrade silently, we fail loudly and require such automations to use
    ``action_type="script"`` (which does honor the stored profile).
    """


def assert_wake_llm_runs_under_default(
    action_type: ActionType | str,
    processing_profile_id: str | None,
    default_profile_id: str | None,
) -> None:
    """Refuse a wake_llm action stamped with a non-default processing profile.

    No-op for scripts, for unstamped/legacy rows, and when the stamped profile is
    the default (the profile a wake_llm actually runs under). Raises
    ``WakeLlmProfileError`` otherwise. ``default_profile_id`` of None disables the
    check (the caller could not determine the default).
    """
    if ActionType(action_type) != ActionType.WAKE_LLM:
        return
    if processing_profile_id is None or default_profile_id is None:
        return
    if processing_profile_id != default_profile_id:
        raise WakeLlmProfileError(
            f"wake_llm automations run under the default profile "
            f"'{default_profile_id}', but this one is stamped with "
            f"'{processing_profile_id}'. wake_llm does not honor a confined "
            "profile, so this would silently escalate privileges. Use "
            'action_type="script" for confined automations.'
        )


async def execute_action(
    db_ctx: DatabaseContext,
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
    default_profile_id: str | None = None,
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
        processing_profile_id: Creating profile for script actions; scripts
            execute under this profile so validation and execution agree.
            wake_llm actions do NOT honor this profile (they run under the task
            worker's default profile), so a non-default profile on a wake_llm
            action is refused via default_profile_id below.
        created_by_user_id: Creating user for script actions; confirm-gated
            tool calls from the script are addressed to this user.
        default_profile_id: The default processing profile id. When provided,
            a wake_llm action stamped with a different profile is refused loudly
            (see assert_wake_llm_runs_under_default).
    """
    if context is None:
        context = {}

    # wake_llm ignores the stored profile at execution time, so refuse to enqueue
    # one stamped with a confined profile rather than silently running as default.
    assert_wake_llm_runs_under_default(
        action_type, processing_profile_id, default_profile_id
    )

    if action_type == ActionType.WAKE_LLM:
        # Prepare callback context
        callback_context = {
            "trigger": context.get("trigger", "Scheduled action"),
            **context,
        }

        # Include any wake context from config
        if "context" in action_config:
            callback_context["message"] = action_config["context"]

        task_id = f"action_{int(time.time() * 1000)}"

        payload: LlmCallbackPayload = {
            "interface_type": interface_type,
            "conversation_id": conversation_id,
            "callback_context": callback_context,
            "scheduling_timestamp": datetime.now(UTC).isoformat(),
        }
        if user_name:
            payload["user_name"] = user_name
        if created_by_user_id is not None:
            payload["created_by_user_id"] = created_by_user_id

        await enqueue_task(
            db_context=db_ctx,
            task_id=task_id,
            task_type="llm_callback",
            payload=payload,
            scheduled_at=scheduled_at,
            recurrence_rule=recurrence_rule,
        )

    elif action_type == ActionType.SCRIPT:
        task_id = f"script_{int(time.time() * 1000)}"

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
