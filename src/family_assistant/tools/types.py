"""
Defines common types used by the tool system, like the execution context.
Moved here to avoid circular imports.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from telegram.ext import Application  # Keep Application for type checking

    from family_assistant.embeddings import EmbeddingGenerator  # Add this import
    from family_assistant.processing import ProcessingService
    from family_assistant.storage.context import DatabaseContext


@dataclass
class ToolExecutionContext:
    """Context passed to tool execution functions."""

    interface_type: str  # e.g., 'telegram', 'web', 'email'
    conversation_id: str  # e.g., Telegram chat ID string, web session UUID
    db_context: "DatabaseContext"
    # calendar_config: dict[str, Any] | None = None  # Removed
    application: Optional["Application"] = None
    # Add other context elements as needed, e.g., timezone_str
    timezone_str: str = "UTC"  # Default, should be overridden
    # Callback to request confirmation from the user interface (e.g., Telegram)
    # This is the signature called by ConfirmingToolsProvider
    request_confirmation_callback: (
        Callable[[str, str, dict[str, Any]], Awaitable[bool]] | None
    ) = None
    # Add processing_service back, make it optional
    processing_service: Optional["ProcessingService"] = None
    embedding_generator: Optional["EmbeddingGenerator"] = (
        None  # Add embedding_generator
    )
    embedding_generator: Optional["EmbeddingGenerator"] = (
        None  # Add embedding_generator
    )


class ToolNotFoundError(LookupError):
    """Custom exception raised when a tool cannot be found by any provider."""

    pass
