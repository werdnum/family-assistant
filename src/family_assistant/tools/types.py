"""
Defines common types used by the tool system, like the execution context.
Moved here to avoid circular imports.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
import asyncio  # Add asyncio for Event type hint

from family_assistant.embeddings import EmbeddingGenerator
from family_assistant.interfaces import ChatInterface  # Import the new interface
from family_assistant.processing import ProcessingService
from family_assistant.storage.context import DatabaseContext


@dataclass
class ToolExecutionContext:
    """
    Context passed to tool execution functions.

    Attributes:
        interface_type: Identifier for the communication interface (e.g., 'telegram', 'web').
        conversation_id: Unique ID for the conversation (e.g., Telegram chat ID string, web session UUID).
        turn_id: Optional ID for the current processing turn.
        db_context: Database context for data access.
        chat_interface: Optional interface for sending messages back to the chat.
        timezone_str: Timezone string for localization, defaults to "UTC".
        request_confirmation_callback: Optional callback to request user confirmation.
            This function is typically called by `ConfirmingToolsProvider`.
            Expected signature:
            (conversation_id: str, interface_type: str, turn_id: str | None,
             prompt_text: str, tool_name: str, tool_args: dict[str, Any], timeout: float)
            -> Awaitable[bool]
        processing_service: Optional service for core processing logic.
        embedding_generator: Optional generator for creating text embeddings.
        new_task_event: Optional event to notify the task worker of new tasks.
    """

    interface_type: str  # e.g., 'telegram', 'web', 'email'
    conversation_id: str  # e.g., Telegram chat ID string, web session UUID
    turn_id: str | None  # The ID of the current processing turn
    db_context: "DatabaseContext"
    chat_interface: Optional["ChatInterface"] = None  # Replaced application
    # Add other context elements as needed, e.g., timezone_str
    timezone_str: str = "UTC"  # Default, should be overridden
    request_confirmation_callback: (
        Callable[
            [
                str,
                str,
                str | None,
                str,
                str,
                dict[str, Any],
                float,
            ],  # Changed chat_id to str
            Awaitable[bool],
        ]
        | None
    ) = None
    # Add processing_service back, make it optional
    processing_service: Optional["ProcessingService"] = None
    embedding_generator: Optional["EmbeddingGenerator"] = (
        None  # Add embedding_generator
    )
    new_task_event: Optional[asyncio.Event] = None  # Add new_task_event


class ToolNotFoundError(LookupError):
    """Custom exception raised when a tool cannot be found by any provider."""

    pass
