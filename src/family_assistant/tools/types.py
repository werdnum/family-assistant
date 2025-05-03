"""
Defines common types used by the tool system, like the execution context.
Moved here to avoid circular imports.
"""

import asyncio
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Callable, Awaitable, TYPE_CHECKING

if TYPE_CHECKING:
    from telegram.ext import Application
    from family_assistant.processing import ProcessingService
    from family_assistant.storage.context import DatabaseContext


@dataclass
class ToolExecutionContext:
    """Context passed to tool execution functions."""

    chat_id: int
    db_context: "DatabaseContext"
    calendar_config: Dict[str, Any]  # Add calendar config
    application: Optional["Application"] = None
    processing_service: Optional["ProcessingService"] = None  # Add processing service
    # Add other context elements as needed, e.g., timezone_str
    timezone_str: str = "UTC"  # Default, should be overridden
    # Callback to request confirmation from the user interface (e.g., Telegram)
    # This is the signature called by ConfirmingToolsProvider
    request_confirmation_callback: Optional[
        Callable[[str, str, Dict[str, Any]], Awaitable[bool]]
    ] = None


class ToolNotFoundError(LookupError):
    """Custom exception raised when a tool cannot be found by any provider."""

    pass
