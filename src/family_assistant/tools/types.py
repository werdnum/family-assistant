"""
Defines common types used by the tool system, like the execution context.
Moved here to avoid circular imports.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import date, datetime

    from family_assistant.camera.protocol import CameraBackend
    from family_assistant.embeddings import EmbeddingGenerator
    from family_assistant.events.indexing_source import IndexingSource
    from family_assistant.events.sources import EventSource
    from family_assistant.home_assistant_wrapper import HomeAssistantClientWrapper
    from family_assistant.interfaces import ChatInterface  # Import the new interface
    from family_assistant.processing import ProcessingService
    from family_assistant.services.attachment_registry import AttachmentRegistry
    from family_assistant.storage.context import DatabaseContext
    from family_assistant.tools.infrastructure import ToolsProvider
    from family_assistant.utils.clock import Clock


class CalDavConfig(TypedDict):
    """CalDAV configuration for calendar access."""

    username: str
    password: str
    base_url: str
    calendar_urls: list[str]


class CalendarConfig(TypedDict):
    """Calendar configuration containing CalDAV settings."""

    caldav: CalDavConfig


class CalendarEvent(TypedDict):
    """Represents a calendar event with structured data."""

    uid: str
    summary: str
    start: datetime | date
    end: datetime | date
    all_day: bool
    calendar_url: str | None
    similarity: float | None


@dataclass
class ToolExecutionContext:
    """
    Context passed to tool execution functions.

    IMPORTANT: Infrastructure fields (processing_service, clock, home_assistant_client,
    event_sources, attachment_registry) have NO defaults to prevent accidental omission.
    You MUST explicitly specify them (even if None) when creating a context.
    This ensures all production sites stay in sync and the type checker catches bugs
    when new infrastructure is added or existing fields are forgotten.

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
        processing_service: Service for core processing logic (REQUIRED - no default).
        embedding_generator: Optional generator for creating text embeddings.
        clock: Clock instance for managing time (REQUIRED - no default).
        indexing_source: Optional indexing event source for emitting document indexing events.
        event_sources: Map of event source ID to source instance (REQUIRED - no default).
        tools_provider: Optional tools provider for direct access (used by execute_script from API).
        home_assistant_client: Home Assistant client wrapper (REQUIRED - no default).
        attachment_registry: Attachment registry for file operations (REQUIRED - no default).
        subconversation_id: Optional ID for delegated subconversations. None indicates main conversation.
            When set, history retrieval is isolated to only messages with this subconversation_id.
    """

    # Core required fields (no defaults)
    interface_type: str  # e.g., 'telegram', 'web', 'email'
    conversation_id: str  # e.g., Telegram chat ID string, web session UUID
    user_name: str  # Name of the user initiating the request
    turn_id: str | None  # The ID of the current processing turn
    db_context: DatabaseContext
    # Infrastructure fields - REQUIRED (no defaults) to catch bugs via type checker
    processing_service: ProcessingService | None  # NO DEFAULT - must specify explicitly
    clock: Clock | None  # NO DEFAULT - must specify explicitly
    home_assistant_client: (
        HomeAssistantClientWrapper | None
    )  # NO DEFAULT - must specify explicitly
    event_sources: dict[str, EventSource] | None  # NO DEFAULT - must specify explicitly
    attachment_registry: (
        AttachmentRegistry | None
    )  # NO DEFAULT - must specify explicitly
    camera_backend: CameraBackend | None  # NO DEFAULT - must specify explicitly
    # Optional fields with defaults (for backward compatibility and convenience)
    user_id: str | None = None  # User identifier
    chat_interface: ChatInterface | None = None  # Replaced application
    chat_interfaces: dict[str, ChatInterface] | None = (
        None  # Dict of interface_type -> ChatInterface for cross-interface messaging
    )
    timezone_str: str = "UTC"  # Timezone string for localization
    processing_profile_id: str | None = (
        None  # Processing profile associated with the request
    )
    subconversation_id: str | None = (
        None  # Subconversation ID for delegated conversations, None for main conversation
    )
    request_confirmation_callback: (
        Callable[
            [
                str,  # interface_type
                str,  # conversation_id
                str | None,  # turn_id
                str,  # tool_name
                str,  # call_id
                # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
                dict[str, Any],  # tool_args
                float,  # timeout
                ToolExecutionContext,  # context (self-reference)
            ],
            Awaitable[bool],
        ]
        | None
    ) = None
    update_activity_callback: Callable[[], None] | None = (
        None  # Optional callback to update task worker activity timestamp
    )
    embedding_generator: EmbeddingGenerator | None = None  # Add embedding_generator
    indexing_source: IndexingSource | None = None  # Add indexing_source
    tools_provider: ToolsProvider | None = None  # Add tools_provider for API access


@dataclass
class ToolAttachment:
    """File attachment for tool results"""

    mime_type: str
    content: bytes | None = None
    file_path: str | None = None
    description: str = ""
    attachment_id: str | None = None  # Populated by infrastructure after storage

    def get_content_as_base64(self) -> str | None:
        """Get content as base64 string for embedding in messages"""
        if self.content is not None:
            return base64.b64encode(self.content).decode()
        return None


@dataclass
class ToolResult:
    """Enhanced tool result supporting multimodal content"""

    text: str | None = None  # Primary text response
    attachments: list[ToolAttachment] | None = (
        None  # List of attachments (can be references or new content)
    )
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    data: dict[str, Any] | list[Any] | str | int | float | bool | None = (
        None  # Structured data for tests/scripts
    )

    def __post_init__(self) -> None:
        """Ensure at least one of text or data is populated"""
        if self.text is None and self.data is None:
            raise ValueError("ToolResult must have either text or data")

    def get_text(self) -> str:
        """
        Get text, generating from data if needed.

        Returns:
            Text representation. If no text field, serializes data as JSON.
        """
        if self.text is not None:
            return self.text

        # Fallback: serialize data as JSON
        if self.data is not None:
            if isinstance(self.data, str):
                return self.data
            return json.dumps(self.data, indent=2, default=str)

        return ""

    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    def get_data(self) -> dict[str, Any] | list[Any] | str | int | float | bool | None:
        """
        Get data, parsing from text if needed.

        Returns:
            Structured data. If no data field, tries to parse text as JSON,
            otherwise returns the text string itself.
        """
        if self.data is not None:
            return self.data

        # Fallback: try to parse text as JSON, else return the string
        if self.text is not None:
            try:
                return json.loads(self.text)  # type: ignore[return-value]
            except (json.JSONDecodeError, TypeError):
                return self.text

        return None

    def to_string(self) -> str:
        """Convert to string for backward compatibility"""
        return self.get_text()  # Use fallback mechanism


# Type alias for tool function return types (backward compatibility)
ToolReturnType = str | ToolResult


class ToolNotFoundError(LookupError):
    """Custom exception raised when a tool cannot be found by any provider."""


def get_attachment_limits(exec_context: ToolExecutionContext) -> tuple[int, int]:
    """
    Get attachment size limits from execution context.

    Args:
        exec_context: Tool execution context

    Returns:
        Tuple of (max_file_size, max_multimodal_size) in bytes
    """
    # Default values
    default_max_file_size = 100 * 1024 * 1024  # 100MB
    default_max_multimodal_size = 20 * 1024 * 1024  # 20MB

    # Try to get from processing service config
    if exec_context.processing_service and hasattr(
        exec_context.processing_service, "app_config"
    ):
        attachment_config = exec_context.processing_service.app_config.get(
            "attachment_config", {}
        )
        max_file_size = attachment_config.get("max_file_size", default_max_file_size)
        max_multimodal_size = attachment_config.get(
            "max_multimodal_size", default_max_multimodal_size
        )
        return max_file_size, max_multimodal_size

    # Fallback to defaults
    return default_max_file_size, default_max_multimodal_size
