"""
Shared action execution logic for both event listeners and scheduled tasks.
"""

import logging
from enum import Enum
from typing import Any

from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.tasks import enqueue_task

logger = logging.getLogger(__name__)


class ActionType(str, Enum):
    """Action types supported by the system."""

    WAKE_LLM = "wake_llm"
    SCRIPT = "script"


async def execute_action(
    db_ctx: DatabaseContext,
    action_type: ActionType,
    action_config: dict[str, Any],
    conversation_id: str,
    interface_type: str = "telegram",
    context: dict[str, Any] | None = None,
) -> None:
    """
    Execute an action. Used by both event listeners and scheduled tasks.

    Args:
        db_ctx: Database context
        action_type: Type of action to execute
        action_config: Configuration for the action
        conversation_id: Conversation to execute in
        interface_type: Interface type (telegram, web, etc)
        context: Additional context (e.g., event data, trigger info)
    """
    import time
    from datetime import datetime, timezone

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

        await enqueue_task(
            db_context=db_ctx,
            task_id=task_id,
            task_type="llm_callback",
            payload={
                "interface_type": interface_type,
                "conversation_id": conversation_id,
                "callback_context": callback_context,
                "scheduling_timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    elif action_type == ActionType.SCRIPT:
        task_id = f"script_{int(time.time() * 1000)}"

        await enqueue_task(
            db_context=db_ctx,
            task_id=task_id,
            task_type="script_execution",
            payload={
                "script_code": action_config.get("script_code", ""),
                "config": action_config,
                "conversation_id": conversation_id,
                **context,
            },
        )
    else:
        raise ValueError(f"Unknown action type: {action_type}")
