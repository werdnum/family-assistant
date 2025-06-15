"""
Defines common types used by the tool system, like the execution context.
Moved here to avoid circular imports.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from family_assistant.embeddings import EmbeddingGenerator
    from family_assistant.events.indexing_source import IndexingSource
    from family_assistant.interfaces import ChatInterface  # Import the new interface
    from family_assistant.processing import ProcessingService
    from family_assistant.storage.context import DatabaseContext
    from family_assistant.utils.clock import Clock


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
        clock: Optional clock instance for managing time.
        indexing_source: Optional indexing event source for emitting document indexing events.
    """

    interface_type: str  # e.g., 'telegram', 'web', 'email'
    conversation_id: str  # e.g., Telegram chat ID string, web session UUID
    user_name: str  # Name of the user initiating the request
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
    clock: Optional["Clock"] = None  # Add clock
    indexing_source: Optional["IndexingSource"] = None  # Add indexing_source
    home_assistant_client: Any | None = None  # Add home_assistant_client


class ToolNotFoundError(LookupError):
    """Custom exception raised when a tool cannot be found by any provider."""
