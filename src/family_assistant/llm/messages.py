"""
Pydantic models for LLM messages.

This module defines typed message classes that replace the previous dict[str, Any] approach.
Each message type has a specific structure enforced by Pydantic validation.

The message types mirror the common structure used by LLM providers (OpenAI, Google, Anthropic)
while adding type safety and runtime validation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NotRequired, Required, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from datetime import datetime

from family_assistant.llm.google_types import GeminiProviderMetadata
from family_assistant.tools.types import (  # noqa: TCH001  # Pydantic needs runtime import for field validation
    ToolAttachment,
    ToolResult,
)

from .content_parts import (
    AttachmentContentPartDict,
    ContentPartDict,
    FileContentPartDict,
    ImageUrlContentPartDict,
    TextContentPartDict,
    attachment_content,
    image_url_content,
    text_content,
)
from .tool_call import (
    ToolCallItem,  # noqa: TCH001  # Pydantic needs runtime import for field validation
)


class ProviderMetadataDict(TypedDict, total=False):
    """Serialized provider metadata structure."""

    provider: Required[str]
    thought_signature: NotRequired[str]


class ToolAttachmentMetadata(TypedDict, total=False):
    """Serialized attachment metadata stored with tool messages."""

    type: Required[str]
    mime_type: Required[str | None]
    description: Required[str | None]
    attachment_id: Required[str | None]
    url: NotRequired[str | None]
    content_url: NotRequired[str | None]
    size: NotRequired[int | None]


# Re-export content part types and helpers for backward compatibility
__all__ = [
    "ContentPartDict",
    "TextContentPartDict",
    "ImageUrlContentPartDict",
    "AttachmentContentPartDict",
    "FileContentPartDict",
    "text_content",
    "image_url_content",
    "attachment_content",
]

# ===== Content Parts =====


class TextContentPart(BaseModel):
    """Text content in a multimodal message."""

    type: Literal["text"]
    text: str

    model_config = ConfigDict(extra="forbid")


class ImageUrlContentPart(BaseModel):
    """Image URL content in a multimodal message."""

    type: Literal["image_url"]
    # ast-grep-ignore: no-dict-any - OpenAI API compatibility
    image_url: dict[str, str]  # {"url": str}

    model_config = ConfigDict(extra="forbid")


class AttachmentContentPart(BaseModel):
    """
    Attachment reference in a message (internal only, converted before LLM).

    This is used for messages with file attachments that need to be processed
    before being sent to the LLM (e.g., converted to inline data or text).
    """

    type: Literal["attachment"]
    attachment_id: str

    model_config = ConfigDict(extra="forbid")


class FileContentPart(BaseModel):
    """
    File placeholder in a message (mock/testing only).

    This is used by mock LLM implementations to represent file references
    that would normally be handled by real LLM clients.
    """

    type: Literal["file_placeholder"]
    # ast-grep-ignore: no-dict-any - Dynamic file reference structure
    file_reference: dict[str, Any]

    model_config = ConfigDict(extra="forbid")


ContentPart = (
    TextContentPart | ImageUrlContentPart | AttachmentContentPart | FileContentPart
)


# ===== Message Types =====


class UserMessage(BaseModel):
    """Message from the user to the LLM."""

    role: Literal["user"] = "user"
    content: str | list[ContentPart]

    # Optional: For provider-specific pre-converted format (e.g., Google GenAI)
    # Excluded from serialization as it's only used during provider conversion
    parts: list[Any] | None = Field(default=None, exclude=True)

    model_config = ConfigDict(extra="forbid")


class AssistantMessage(BaseModel):
    """Message from the LLM assistant.

    This is a pure application-layer type - tool_calls must be properly typed ToolCallItem objects.
    Deserialization from database dicts happens explicitly in the repository layer.
    """

    role: Literal["assistant"] = "assistant"
    content: str | None = None
    tool_calls: list[ToolCallItem] | None = None
    # ast-grep-ignore: no-dict-any - Accepts both dicts (for serialization) and provider metadata objects (e.g., GeminiProviderMetadata)
    provider_metadata: Any | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("tool_calls", mode="after")
    @classmethod
    def check_has_content_or_tool_calls(
        cls,
        tool_calls: list[ToolCallItem] | None,
        info: Any,  # noqa: ANN401
    ) -> list[ToolCallItem] | None:
        """Ensure assistant message has either content or tool_calls."""
        content = info.data.get("content")
        if content is None and tool_calls is None:
            raise ValueError("Assistant message must have content or tool_calls")
        return tool_calls


class ToolMessage(BaseModel):
    """
    Message representing a tool execution result.

    This is sent back to the LLM after a tool is executed.
    """

    role: Literal["tool"] = "tool"
    tool_call_id: str
    content: str
    name: str  # Function name
    error_traceback: str | None = None
    # Provider-specific metadata (e.g., Gemini thought signatures)
    provider_metadata: GeminiProviderMetadata | ProviderMetadataDict | None = None

    # IMPORTANT: Preserve the original ToolResult for better tracking and debugging
    # This field is excluded from serialization and used internally
    tool_result: ToolResult | None = Field(default=None, exclude=True)

    # Transient field for provider processing (e.g., attachment injection)
    # Excluded from serialization as it's only used during provider conversion
    transient_attachments: list[ToolAttachment] | None = Field(
        default=None, exclude=True, alias="_attachments"
    )

    # Attachment metadata for database storage (serialized)
    attachments: list[ToolAttachmentMetadata] | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SystemMessage(BaseModel):
    """System prompt message."""

    role: Literal["system"] = "system"
    content: str

    model_config = ConfigDict(extra="forbid")


class ErrorMessage(BaseModel):
    """
    Error message (database-only).

    Used to record errors in conversation history.
    """

    role: Literal["error"] = "error"
    content: str
    error_traceback: str | None = None

    model_config = ConfigDict(extra="forbid")


# ===== Union Type =====

LLMMessage = UserMessage | AssistantMessage | ToolMessage | SystemMessage | ErrorMessage


# ===== Message with Metadata =====


@dataclass
class MessageWithMetadata:
    """
    Container for LLMMessage with database metadata.

    Used when processing needs access to both the typed message content
    and database-specific fields like internal_id and interface_message_id.
    """

    message: LLMMessage
    internal_id: str
    interface_message_id: str | None
    timestamp: datetime
    conversation_id: str
    interface_type: str
    user_id: str | None = None
    turn_id: str | None = None
    thread_root_id: int | None = None

    def __post_init__(self) -> None:
        """Validate that message is a proper LLMMessage type."""
        if not isinstance(
            self.message,
            (UserMessage, AssistantMessage, ToolMessage, SystemMessage, ErrorMessage),
        ):
            raise TypeError(f"message must be LLMMessage, got {type(self.message)}")


# ===== Utility Functions =====


# Helper to serialize typed tool calls into JSON-safe dicts
def _serialize_tool_call_item(tool_call: ToolCallItem) -> dict[str, object]:
    """Serialize a ToolCallItem into a JSON-safe dict."""
    # Ensure arguments is a JSON string
    args = tool_call.function.arguments
    if not isinstance(args, str):
        args = json.dumps(args)

    provider_metadata = None
    if tool_call.provider_metadata is not None:
        provider_metadata = (
            tool_call.provider_metadata.to_dict()
            if hasattr(tool_call.provider_metadata, "to_dict")
            else tool_call.provider_metadata
        )

    tc_dict = {
        "id": tool_call.id,
        "type": tool_call.type,
        "function": {
            "name": tool_call.function.name,
            "arguments": args,
        },
    }

    if provider_metadata is not None:
        tc_dict["provider_metadata"] = provider_metadata

    return tc_dict


def message_to_json_dict(msg: LLMMessage) -> dict[str, object]:
    """
    Convert a message to a fully serialized dictionary suitable for JSON encoding.

    This recursively serializes all nested objects including ToolCallItem objects,
    making the result safe for json.dumps().
    """
    if isinstance(msg, UserMessage):
        return {
            "role": "user",
            "content": [
                part.model_dump(mode="json", exclude_none=True) for part in msg.content
            ]
            if isinstance(msg.content, list)
            else msg.content,
        }

    if isinstance(msg, AssistantMessage):
        tool_calls = (
            [_serialize_tool_call_item(tc) for tc in msg.tool_calls]
            if msg.tool_calls
            else None
        )

        provider_metadata = msg.provider_metadata
        if isinstance(provider_metadata, dict):
            pass
        elif isinstance(provider_metadata, GeminiProviderMetadata):
            provider_metadata = provider_metadata.to_dict()

        message_dict: dict[str, object | None] = {
            "role": "assistant",
            "content": msg.content,
            "tool_calls": tool_calls,
        }
        if provider_metadata is not None:
            message_dict["provider_metadata"] = provider_metadata
        return message_dict

    if isinstance(msg, ToolMessage):
        provider_metadata = msg.provider_metadata
        if isinstance(provider_metadata, dict):
            pass
        elif isinstance(provider_metadata, GeminiProviderMetadata):
            provider_metadata = provider_metadata.to_dict()

        tool_message_dict: dict[str, object | None] = {
            "role": "tool",
            "tool_call_id": msg.tool_call_id,
            "content": msg.content,
            "name": msg.name,
            "error_traceback": msg.error_traceback,
        }
        if provider_metadata is not None:
            tool_message_dict["provider_metadata"] = provider_metadata
        return tool_message_dict

    if isinstance(msg, SystemMessage):
        return {
            "role": "system",
            "content": msg.content,
        }

    if isinstance(msg, ErrorMessage):
        return {
            "role": "error",
            "content": msg.content,
            "error_traceback": msg.error_traceback,
        }

    # If we ever hit this, the caller is providing an unsupported type.
    raise TypeError(f"Unsupported message type for serialization: {type(msg)}")


# Backwards-compatible alias for callers still expecting message_to_dict
def message_to_dict(msg: LLMMessage) -> dict[str, object]:
    """
    Serialize a typed LLMMessage to a JSON-safe dict.

    This intentionally accepts only typed message objects to keep APIs strict;
    callers must perform deserialization before invoking this helper.
    """
    return message_to_json_dict(msg)


# ast-grep-ignore: no-dict-any - Generic deserialization function
def dict_to_message(data: dict[str, Any]) -> LLMMessage:
    """
    Convert a dictionary to a typed message.

    This function examines the 'role' field and constructs the appropriate
    message type. It validates the structure and raises ValidationError if invalid.

    Args:
        data: Dictionary with message data (must have 'role' field)

    Returns:
        Appropriate LLMMessage subclass

    Raises:
        KeyError: If 'role' field is missing
        ValidationError: If message structure is invalid
    """
    role = data["role"]

    if role == "user":
        return UserMessage(**data)
    elif role == "assistant":
        return AssistantMessage(**data)
    elif role == "tool":
        return ToolMessage(**data)
    elif role == "system":
        return SystemMessage(**data)
    elif role == "error":
        return ErrorMessage(**data)
    else:
        raise ValueError(f"Unknown message role: {role}")


# ast-grep-ignore: no-dict-any - Conversion function for ToolResult
def tool_result_to_llm_message(
    result: ToolResult,
    tool_call_id: str,
    function_name: str,
    provider_metadata: GeminiProviderMetadata | ProviderMetadataDict | None = None,
) -> ToolMessage:
    """
    Convert a ToolResult to a ToolMessage for LLM consumption.

    This includes the _attachments field for provider handling.

    Args:
        result: The ToolResult to convert
        tool_call_id: The tool call ID
        function_name: The function name
        provider_metadata: Provider-specific metadata (e.g., Gemini thought signature)

    Returns:
        ToolMessage with attachments for provider processing
    """
    # Prepare attachment metadata for database storage
    attachments_metadata: list[ToolAttachmentMetadata] | None = None
    if result.attachments:
        attachments_metadata = []
        for att in result.attachments:
            attachments_metadata.append({
                "type": "tool_result",
                "mime_type": att.mime_type,
                "description": att.description,
                "attachment_id": att.attachment_id,  # Include ID for references
            })

    serialized_provider_metadata: (
        GeminiProviderMetadata | ProviderMetadataDict | None
    ) = None
    if isinstance(provider_metadata, (GeminiProviderMetadata, dict)):
        serialized_provider_metadata = provider_metadata

    return ToolMessage(
        tool_call_id=tool_call_id,
        content=result.get_text(),  # Use fallback mechanism
        name=function_name,
        provider_metadata=serialized_provider_metadata,
        tool_result=result,  # Preserve original ToolResult for debugging
        _attachments=result.attachments,  # Pass attachments to provider (using alias)
        attachments=attachments_metadata,  # Store metadata in database
    )
