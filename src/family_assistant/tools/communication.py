"""Communication and messaging tools.

This module contains tools for sending messages to users and retrieving
conversation history.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


# Tool Definitions
COMMUNICATION_TOOLS_DEFINITION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_message_history",
            "description": (
                "Retrieve past messages from the current conversation history. Use this if you need context from earlier in the conversation that might not be in the default short-term history window."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Optional. The maximum number of messages to retrieve (most recent first). Default is 10."
                        ),
                        "default": 10,
                    },
                    "max_age_hours": {
                        "type": "integer",
                        "description": (
                            "Optional. Retrieve messages only up to this many hours old. Default is 24."
                        ),
                        "default": 24,
                    },
                },
                "required": [],  # No parameters are strictly required, defaults will be used
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message_to_user",
            "description": "Sends a textual message to another known user on Telegram. You MUST use their Chat ID as the target, which is provided in the 'Known users' section of the system prompt.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_chat_id": {
                        "type": "integer",
                        "description": "The unique Telegram Chat ID of the user to send the message to. This ID must be one of the known users provided in the system context.",
                    },
                    "message_content": {
                        "type": "string",
                        "description": "The content of the message to send to the user.",
                    },
                },
                "required": ["target_chat_id", "message_content"],
            },
        },
    },
]


# Tool Implementations
async def get_message_history_tool(
    exec_context: ToolExecutionContext,
    limit: int = 10,
    max_age_hours: int = 24,
) -> str:
    """
    Retrieves recent message history for the current chat, with optional filters.

    Args:
        exec_context: The execution context containing chat_id and db_context.
        limit: Maximum number of messages to retrieve (default: 10).
        max_age_hours: Maximum age of messages in hours (default: 24).

    Returns:
        A formatted string containing the message history or an error message.
    """
    from family_assistant.storage import get_recent_history

    # Use new identifiers
    interface_type = exec_context.interface_type
    conversation_id = exec_context.conversation_id
    db_context = exec_context.db_context
    logger.info(
        f"Executing get_message_history_tool for {interface_type}:{conversation_id} (limit={limit}, max_age_hours={max_age_hours})"
    )

    try:
        max_age_delta = timedelta(hours=max_age_hours)
        history_messages = await get_recent_history(
            db_context=db_context,  # Pass context
            interface_type=interface_type,  # Pass interface type
            conversation_id=conversation_id,  # Pass conversation ID
            limit=limit,
            max_age=max_age_delta,
        )

        if not history_messages:
            return "No message history found matching the specified criteria."

        # Format the history for the LLM
        formatted_history = ["Retrieved message history:"]
        for msg in history_messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            timestamp = msg.get("timestamp")
            time_str = (
                timestamp.strftime("%Y-%m-%d %H:%M:%S %Z")
                if timestamp
                else "Unknown Time"
            )

            # Basic formatting, include full content
            formatted_history.append(f"[{time_str}] {role.capitalize()}: {content}")

            # Include tool call info if present (simplified)
            if role == "assistant" and msg.get(
                "tool_calls"
            ):  # Use correct key 'tool_calls'
                tool_calls = msg.get("tool_calls_info_raw", [])
                if isinstance(tool_calls, list):
                    for call in tool_calls:
                        if isinstance(call, dict):
                            func_name = call.get("function_name", "unknown_tool")
                            args = call.get("arguments", {})
                            resp = call.get("response_content", "")
                            formatted_history.append(
                                f"  -> Called Tool: {func_name}({json.dumps(args)}) -> Response: {resp}"
                            )  # Include full response

        return "\n".join(formatted_history)

    except Exception as e:
        logger.error(
            f"Error executing get_message_history_tool for {interface_type}:{conversation_id}: {e}",
            exc_info=True,
        )
        return f"Error: Failed to retrieve message history. {e}"


async def send_message_to_user_tool(
    exec_context: ToolExecutionContext, target_chat_id: int, message_content: str
) -> str:
    """
    Sends a message to another known user via Telegram.

    Args:
        exec_context: The execution context.
        target_chat_id: The Telegram Chat ID of the recipient.
        message_content: The text of the message to send.

    Returns:
        A string indicating success or failure.
    """
    from family_assistant import storage

    logger.info(
        f"Executing send_message_to_user_tool to chat_id {target_chat_id} with content: '{message_content[:50]}...'"
    )
    chat_interface = exec_context.chat_interface
    db_context = exec_context.db_context
    # The turn_id from the exec_context is the ID of the turn that *requested* this tool call.
    # This is useful for linking the sent message back to the originating interaction.
    requesting_turn_id = exec_context.turn_id

    if not chat_interface:
        logger.error(
            "ChatInterface not available in ToolExecutionContext for send_message_to_user_tool."
        )
        return "Error: Chat interface not available."

    try:
        # Use the ChatInterface to send the message.
        # Assuming the target_chat_id is for the same interface type as the current context.
        # The TelegramChatInterface will handle converting target_chat_id to int.
        sent_message_id_str = await chat_interface.send_message(
            conversation_id=str(target_chat_id),  # Pass as string
            text=message_content,
            # parse_mode can be added if needed, default is plain text
        )

        if not sent_message_id_str:
            logger.error(
                f"Failed to send message to chat_id {target_chat_id} via ChatInterface."
            )
            return f"Error: Could not send message to Chat ID {target_chat_id} (sending failed)."

        logger.info(
            f"Message sent to chat_id {target_chat_id}. Interface Message ID: {sent_message_id_str}"
        )

        # Record the sent message in history for the target user's chat
        try:
            await storage.add_message_to_history(
                db_context=db_context,
                interface_type="telegram",  # Assuming Telegram interface for now
                conversation_id=str(
                    target_chat_id
                ),  # History is for the target user's conversation
                interface_message_id=sent_message_id_str,
                turn_id=requesting_turn_id,  # Link to the turn that initiated this action
                thread_root_id=None,  # This message likely starts a new interaction or is standalone in the target chat
                timestamp=datetime.now(timezone.utc),
                role="assistant",  # The bot is the one sending this message to the target user
                content=message_content,
                tool_calls=None,
                tool_call_id=None,
                reasoning_info={
                    "source_turn_id": requesting_turn_id,
                    "tool_name": "send_message_to_user",
                },  # Optional: add reasoning
                error_traceback=None,
                processing_profile_id=getattr(
                    exec_context, "processing_profile_id", None
                ),
            )
            logger.info(
                f"Message sent to chat_id {target_chat_id} was recorded in history."
            )
            return f"Message sent successfully to user with Chat ID {target_chat_id}."
        except Exception as db_err:
            logger.error(
                f"Message sent to chat_id {target_chat_id}, but failed to record in history: {db_err}",
                exc_info=True,
            )
            # Still return success for sending, but note the history failure.
            return f"Message sent to user with Chat ID {target_chat_id}, but failed to record in history."

    except Exception as e:
        logger.error(
            f"Failed to send message to chat_id {target_chat_id}: {e}", exc_info=True
        )
        return (
            f"Error: Could not send message to Chat ID {target_chat_id}. Details: {e}"
        )
