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
            Ignored for wake_llm actions, which run under the event handler
            profile.
        created_by_user_id: Creating user for script actions; confirm-gated
            tool calls from the script are addressed to this user.
    """
    if context is None:
        context = {}

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
