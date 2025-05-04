"""
Defines common types used by the tool system, like the execution context.
Moved here to avoid circular imports.
"""

import asyncio
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Callable, Awaitable, TYPE_CHECKING

if TYPE_CHECKING:
    from telegram.ext import Application # Keep Application for type checking
    from family_assistant.processing import ProcessingService
    from family_assistant.storage.context import DatabaseContext


@dataclass
class ToolExecutionContext:
    """Context passed to tool execution functions."""

    interface_type: str # e.g., 'telegram', 'web', 'email'
    conversation_id: str # e.g., Telegram chat ID string, web session UUID
    db_context: "DatabaseContext"
    calendar_config: Dict[str, Any]  # Add calendar config
    application: Optional["Application"] = None
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
