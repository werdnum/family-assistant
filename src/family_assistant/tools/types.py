"""
Defines common types used by the tool system, like the execution context.
Moved here to avoid circular imports.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NotRequired, Protocol, TypedDict

# Note: CalendarConfig TypedDict kept here for backward compatibility with tool functions
# The Pydantic CalendarConfig in config_models.py is used for config file validation


class CalDavConfig(TypedDict, total=False):
    """CalDAV configuration for calendar access."""

    username: str | None
    password: str | None
    calendar_urls: list[str]
    base_url: str | None


class ICalConfig(TypedDict, total=False):
    """iCal URL configuration."""

    urls: list[str]


class CalendarConfig(TypedDict, total=False):
    """Calendar configuration used by tools."""

    caldav: CalDavConfig | None
    ical: ICalConfig | None


class MCPServerStdIOConfig(TypedDict, total=False):
    """Configuration for a stdio-based MCP server."""

    transport: Literal["stdio"]
    command: str
    args: list[str]
    env: dict[str, str]
    tool_metadata: dict[str, list[str]]


class MCPServerSSEConfig(TypedDict):
    """Configuration for an SSE-based MCP server."""

    transport: Literal["sse"]
    url: str
    token: NotRequired[str | None]
    tool_metadata: NotRequired[dict[str, list[str]]]


class MCPServerStreamableHTTPConfig(TypedDict):
    """Configuration for a Streamable HTTP-based MCP server.

    The ``transport`` field accepts ``"streamable_http"`` (canonical) as well as
    the ``"streamablehttp"`` and ``"http"`` aliases.
    """

    transport: Literal["streamable_http", "streamablehttp", "http"]
    url: str
    token: NotRequired[str | None]
    tool_metadata: NotRequired[dict[str, list[str]]]


class MCPServerGenericConfig(TypedDict, total=False):
    """Generic configuration for MCP servers, used when transport is not explicitly specified."""

    transport: str
    command: str
    args: list[str]
    env: dict[str, str]
    url: str
    token: str
    tool_metadata: dict[str, list[str]]


# Use a Union to represent the allowed MCP server configurations.
MCPServerConfig = (
    MCPServerStdIOConfig
    | MCPServerSSEConfig
    | MCPServerStreamableHTTPConfig
    | MCPServerGenericConfig
)


# Tool Definition Types
# These TypedDicts provide proper typing for LLM tool definitions,
# following the OpenAI function calling schema format (camelCase JSON Schema).
#
# Design rationale for using dict[str, Any] in nested structures:
# 1. JSON Schema allows arbitrary recursive nesting (objects containing arrays
#    of objects containing more objects, etc.). Python's type system doesn't
#    elegantly handle recursive TypedDicts.
# 2. Google genai's SchemaDict uses snake_case (max_items, additional_properties)
#    while OpenAI-style tools use camelCase (maxItems, additionalProperties),
#    making those types incompatible without conversion.
# 3. No standard library provides TypedDict types for camelCase JSON Schema.
#
# Our approach: strict typing for the top-level structure we control
# (ToolDefinition, ToolFunctionSchema, ToolParametersSchema), with dict[str, Any]
# for deeply nested property definitions where JSON Schema's flexibility exceeds
# what TypedDict can practically express.


class ToolPropertyItems(TypedDict, total=False):
    """Schema for array item definitions in tool parameters.

    For complex nested objects, additional fields may be present.
    """

    type: str
    description: str
    # ast-grep-ignore: no-dict-any - JSON Schema nested properties can be arbitrarily deep
    properties: dict[str, Any]  # Nested object properties
    required: list[str]  # Required fields for nested objects
    # ast-grep-ignore: no-dict-any - JSON Schema array items can be arbitrarily nested
    items: dict[str, Any]  # For nested arrays
    enum: list[str]
    minItems: int
    maxItems: int


class ToolPropertySchema(TypedDict, total=False):
    """Schema for individual parameter properties in tool definitions.

    Follows JSON Schema format as used by OpenAI function calling.
    """

    type: str
    description: str
    format: str
    default: Any
    enum: list[str]
    items: ToolPropertyItems
    # ast-grep-ignore: no-dict-any - JSON Schema nested properties can be arbitrarily deep
    properties: dict[str, Any]  # For nested object types
    required: list[str]  # For nested object types
    additionalProperties: bool
    minItems: int
    maxItems: int


class ToolParametersSchema(TypedDict, total=False):
    """Schema for the parameters object in tool definitions.

    Follows JSON Schema 'object' type format.
    """

    type: str
    properties: dict[str, ToolPropertySchema]
    required: list[str]
    additionalProperties: bool


class ToolFunctionSchema(TypedDict):
    """Schema for the function definition within a tool.

    Contains the function name, description, and parameters schema.
    """

    name: str
    description: str
    parameters: ToolParametersSchema


class ToolDefinition(TypedDict):
    """Schema for a complete tool definition.

    This is the top-level structure passed to LLMs for function calling.
    """

    type: str
    function: ToolFunctionSchema


if TYPE_CHECKING:
    from datetime import date, datetime
    from zoneinfo import ZoneInfo

    from family_assistant.camera.protocol import CameraBackend
    from family_assistant.config_models import AppConfig
    from family_assistant.embeddings import EmbeddingGenerator
    from family_assistant.events.indexing_source import IndexingSource
    from family_assistant.events.sources import EventSource
    from family_assistant.home_assistant_wrapper import HomeAssistantClientWrapper
    from family_assistant.interfaces import ChatInterface  # Import the new interface
    from family_assistant.processing import ProcessingService
    from family_assistant.services.attachment_registry import AttachmentRegistry
    from family_assistant.services.confirmation_waiters import (
        ConfirmationResultWaiterRegistry,
    )
    from family_assistant.skills.registry import NoteRegistry
    from family_assistant.storage.context import DatabaseContext
    from family_assistant.storage.repositories.notes import NoteWritePolicy
    from family_assistant.telegram.protocols import ConfirmationUIManager
    from family_assistant.tools.infrastructure import ToolsProvider
    from family_assistant.utils.clock import Clock


# Maps event source IDs to their corresponding EventSource instances.
type EventSourcesById = Mapping[str, EventSource]
type ToolArgumentValue = (
    str
    | int
    | float
    | bool
    | None
    | Sequence[ToolArgumentValue]
    | Mapping[str, ToolArgumentValue]
)
type ToolArguments = dict[str, ToolArgumentValue]
type ToolArgumentsView = Mapping[str, ToolArgumentValue]


@dataclass(frozen=True)
class ConfirmationOutcome:
    """Terminal result of a user confirmation request."""

    kind: Literal[
        "approved",
        "completed",
        "rejected",
        "timed_out",
        "cancelled",
        "failed",
    ]
    result: str | ToolResult | None = None


class RequestConfirmationCallback(Protocol):
    """Callback interface for user confirmation requests."""

    async def __call__(
        self,
        interface_type: str,
        conversation_id: str,
        turn_id: str | None,
        tool_name: str,
        call_id: str,
        tool_args: ToolArguments,
        timeout_seconds: float,
        context: ToolExecutionContext,
    ) -> ConfirmationOutcome:
        """Request confirmation for a tool action."""
        ...


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
        timezone: ZoneInfo for localization, defaults to UTC.
        request_confirmation_callback: Optional callback to request user confirmation.
            This function is typically called by `PolicyEnforcingToolsProvider`.
            Expected signature:
            (interface_type: str, conversation_id: str, turn_id: str | None,
             tool_name: str, call_id: str, tool_args: ToolArguments,
             timeout_seconds: float, context: ToolExecutionContext)
            -> Awaitable[ConfirmationOutcome]
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
    event_sources: EventSourcesById | None  # NO DEFAULT - must specify explicitly
    attachment_registry: (
        AttachmentRegistry | None
    )  # NO DEFAULT - must specify explicitly
    camera_backend: CameraBackend | None  # NO DEFAULT - must specify explicitly
    timezone: ZoneInfo  # NO DEFAULT - must specify explicitly
    # Optional fields with defaults (for backward compatibility and convenience)
    user_id: str | None = None  # User identifier
    chat_interface: ChatInterface | None = None  # Replaced application
    chat_interfaces: dict[str, ChatInterface] | None = (
        None  # Dict of interface_type -> ChatInterface for cross-interface messaging
    )
    processing_profile_id: str | None = (
        None  # Processing profile associated with the request
    )
    subconversation_id: str | None = (
        None  # Subconversation ID for delegated conversations, None for main conversation
    )
    request_confirmation_callback: RequestConfirmationCallback | None = None
    update_activity_callback: Callable[[], None] | None = (
        None  # Optional callback to update task worker activity timestamp
    )
    embedding_generator: EmbeddingGenerator | None = None  # Add embedding_generator
    indexing_source: IndexingSource | None = None  # Add indexing_source
    tools_provider: ToolsProvider | None = None  # Add tools_provider for API access
    visibility_grants: set[str] | None = None
    default_note_visibility_labels: list[str] | None = None
    required_note_visibility_labels: list[str] | None = None
    allowed_note_visibility_labels: list[str] | None = None
    allow_wake_llm: bool = True
    note_registry: NoteRegistry | None = None
    confirmation_result_waiters: ConfirmationResultWaiterRegistry | None = None
    confirmation_ui_managers: dict[str, ConfirmationUIManager] | None = None
    in_script: bool = False
    """True when this context is executing inside a script (MontyEngine).

    Tools that would otherwise defer their result to a later conversation message
    (notably ``delegate_to_service``'s async handoff) must run synchronously and
    return their result directly so the script can use it.
    """

    def note_write_policy(self) -> NoteWritePolicy:
        """Derive the note write policy for the active profile from this context.

        Every note writer in the tool layer routes through this so label
        confinement is applied uniformly. The four fields flow from the active
        profile's ``ProcessingConfig``.
        """
        from family_assistant.storage.repositories.notes import (  # noqa: PLC0415
            NoteWritePolicy,
        )

        return NoteWritePolicy(
            visibility_grants=self.visibility_grants,
            default_labels=self.default_note_visibility_labels,
            required_labels=self.required_note_visibility_labels,
            allowed_labels=self.allowed_note_visibility_labels,
        )


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
    # ast-grep-ignore: no-dict-any - tool result data supports multiple shapes from different tools
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

    # ast-grep-ignore: no-dict-any - return type mirrors data field which accepts multiple shapes
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
        app_config: AppConfig | None = exec_context.processing_service.app_config
        if app_config:
            attachment_config = app_config.attachment_config
            return (
                attachment_config.max_file_size,
                attachment_config.max_multimodal_size,
            )

    # Fallback to defaults
    return default_max_file_size, default_max_multimodal_size
