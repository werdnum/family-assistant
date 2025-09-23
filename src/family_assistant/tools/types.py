"""
Defines common types used by the tool system, like the execution context.
Moved here to avoid circular imports.
"""

import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from family_assistant.embeddings import EmbeddingGenerator
    from family_assistant.events.indexing_source import IndexingSource
    from family_assistant.events.sources import EventSource
    from family_assistant.home_assistant_wrapper import HomeAssistantClientWrapper
    from family_assistant.interfaces import ChatInterface  # Import the new interface
    from family_assistant.processing import ProcessingService
    from family_assistant.storage.context import DatabaseContext
    from family_assistant.tools import ToolsProvider
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
        update_activity_callback: Optional callback to update task worker activity timestamp.
            Used by long-running tasks to prevent worker from being marked as stuck.
        processing_service: Optional service for core processing logic.
        embedding_generator: Optional generator for creating text embeddings.
        clock: Optional clock instance for managing time.
        indexing_source: Optional indexing event source for emitting document indexing events.
        event_sources: Optional map of event source ID to source instance for validation.
        tools_provider: Optional tools provider for direct access (used by execute_script from API).
    """

    interface_type: str  # e.g., 'telegram', 'web', 'email'
    conversation_id: str  # e.g., Telegram chat ID string, web session UUID
    user_name: str  # Name of the user initiating the request
    turn_id: str | None  # The ID of the current processing turn
    db_context: "DatabaseContext"
    chat_interface: Optional["ChatInterface"] = None  # Replaced application
    timezone_str: str = "UTC"  # Timezone string for localization
    # Processing profile associated with the request
    processing_profile_id: str | None = None
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
    update_activity_callback: Callable[[], None] | None = (
        None  # Optional callback to update task worker activity timestamp
    )
    # Add processing_service back, make it optional
    processing_service: Optional["ProcessingService"] = None
    embedding_generator: Optional["EmbeddingGenerator"] = (
        None  # Add embedding_generator
    )
    clock: Optional["Clock"] = None  # Add clock
    indexing_source: Optional["IndexingSource"] = None  # Add indexing_source
    home_assistant_client: Optional["HomeAssistantClientWrapper"] = (
        None  # Add home_assistant_client
    )
    event_sources: dict[str, "EventSource"] | None = (
        None  # Map of event source ID to source instance
    )
    tools_provider: Optional["ToolsProvider"] = (
        None  # Add tools_provider for API access
    )


@dataclass
class ToolAttachment:
    """File attachment for tool results"""

    mime_type: str
    content: bytes | None = None
    file_path: str | None = None
    description: str = ""

    def get_content_as_base64(self) -> str | None:
        """Get content as base64 string for embedding in messages"""
        if self.content is not None:
            return base64.b64encode(self.content).decode()
        return None


@dataclass
class ToolResult:
    """Enhanced tool result supporting multimodal content"""

    text: str  # Primary text response
    attachment: ToolAttachment | None = None

    def to_string(self) -> str:
        """Convert to string for backward compatibility"""
        return self.text  # Message injection handled by providers

    def to_llm_message(
        self,
        tool_call_id: str,
        function_name: str,
    ) -> dict[str, Any]:
        """Convert to message format for LLM (includes _attachment for provider handling)"""
        message = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": self.text,
            "error_traceback": None,
            "name": function_name,  # OpenAI API compatibility
        }

        # Add attachment metadata for provider to handle
        if self.attachment:
            message["_attachment"] = self.attachment
            # Store in history metadata
            message["attachments"] = [
                {
                    "type": "tool_result",
                    "mime_type": self.attachment.mime_type,
                    "description": self.attachment.description,
                }
            ]

        return message

    def to_history_message(
        self,
        tool_call_id: str,
        function_name: str,
    ) -> dict[str, Any]:
        """Convert to message format for database history (excludes raw attachment data)"""
        llm_message = self.to_llm_message(tool_call_id, function_name)
        history_message = llm_message.copy()

        # Remove raw attachment data but keep metadata
        history_message.pop("_attachment", None)
        history_message["tool_name"] = function_name  # Store tool name for database

        return history_message


# Type alias for tool function return types (backward compatibility)
ToolReturnType = str | ToolResult


class ToolNotFoundError(LookupError):
    """Custom exception raised when a tool cannot be found by any provider."""
