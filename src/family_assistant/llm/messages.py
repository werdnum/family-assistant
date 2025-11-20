"""
Pydantic models for LLM messages.

This module defines typed message classes that replace the previous dict[str, Any] approach.
Each message type has a specific structure enforced by Pydantic validation.

The message types mirror the common structure used by LLM providers (OpenAI, Google, Anthropic)
while adding type safety and runtime validation.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Import google_types for deserializing provider_metadata in tool calls
# This import must be at runtime (not TYPE_CHECKING) because it's used in the
# deserialize_tool_calls field validator, which needs the actual class to call from_dict().
# The linter suggests TYPE_CHECKING, but that would cause runtime errors in the validator.
from family_assistant.llm.google_types import (  # noqa: TCH001
    GeminiProviderMetadata,
)

# These imports must be at runtime (not TYPE_CHECKING) because Pydantic needs them
# for field validation in ToolMessage. The linter suggests TYPE_CHECKING, but that
# would cause "model not fully defined" errors at runtime.
from family_assistant.tools.types import (  # noqa: TCH001
    ToolAttachment,
    ToolResult,
)

from .tool_call import ToolCallFunction, ToolCallItem  # noqa: TCH001

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
    """Message from the LLM assistant."""

    role: Literal["assistant"] = "assistant"
    content: str | None = None
    tool_calls: list[ToolCallItem] | None = None
    # ast-grep-ignore: no-dict-any - Accepts both dicts (for serialization) and provider metadata objects (e.g., GeminiProviderMetadata)
    provider_metadata: Any | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("tool_calls", mode="before")
    @classmethod
    # ast-grep-ignore: no-dict-any - Deserialization from database JSON
    def deserialize_tool_calls(
        cls,
        # ast-grep-ignore: no-dict-any - Deserialization from database JSON
        tool_calls: list[ToolCallItem] | list[dict[str, Any]] | None,
    ) -> list[ToolCallItem] | None:
        """Deserialize tool_calls from dict format (from database) to ToolCallItem objects."""
        if tool_calls is None:
            return None

        result = []
        for tc in tool_calls:
            # If already a ToolCallItem, keep it
            if isinstance(tc, ToolCallItem):
                result.append(tc)
                continue

            # Convert dict to ToolCallItem with properly typed provider_metadata
            if not isinstance(tc, dict):
                raise ValueError(f"Expected ToolCallItem or dict, got {type(tc)}")

            # Deserialize provider_metadata if present
            provider_metadata = tc.get("provider_metadata")
            if (
                provider_metadata
                and isinstance(provider_metadata, dict)
                and provider_metadata.get("provider") == "google"
            ):
                # Deserialize Google provider metadata
                provider_metadata = GeminiProviderMetadata.from_dict(provider_metadata)
                # Add other providers here as needed

            # Deserialize function
            func_data = tc.get("function")
            if isinstance(func_data, dict):
                func = ToolCallFunction(
                    name=func_data["name"], arguments=func_data["arguments"]
                )
            elif isinstance(func_data, ToolCallFunction):
                func = func_data
            else:
                raise ValueError(f"Invalid function data: {func_data}")

            # Create ToolCallItem
            result.append(
                ToolCallItem(
                    id=tc["id"],
                    type=tc["type"],
                    function=func,
                    provider_metadata=provider_metadata,
                )
            )

        return result

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

    # IMPORTANT: Preserve the original ToolResult for better tracking and debugging
    # This field is excluded from serialization and used internally
    tool_result: ToolResult | None = Field(default=None, exclude=True)

    # Transient field for provider processing (e.g., attachment injection)
    # Excluded from serialization as it's only used during provider conversion
    transient_attachments: list[ToolAttachment] | None = Field(
        default=None, exclude=True, alias="_attachments"
    )

    # Attachment metadata for database storage (serialized)
    # ast-grep-ignore: no-dict-any - Database serialization format
    attachments: list[dict[str, Any]] | None = None

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


# ===== Utility Functions =====


# ast-grep-ignore: no-dict-any - Generic serialization function
def message_to_dict(msg: LLMMessage | dict[str, Any]) -> dict[str, Any]:
    """
    Convert a message to a dictionary.

    This uses Pydantic's model_dump() which automatically excludes
    fields marked with exclude=True (_attachments, tool_result, parts).
    If the message is already a dict, it's returned as-is.

    IMPORTANT: For AssistantMessage with tool_calls, preserves ToolCallItem objects
    instead of converting them to dicts. This ensures type safety throughout the
    message processing pipeline.

    Args:
        msg: The message to convert (can be a Pydantic message or dict)

    Returns:
        Dictionary representation with ToolCallItem objects preserved
    """
    if isinstance(msg, dict):
        return msg

    # Use model_dump to get the base dict
    result = msg.model_dump(mode="python", exclude_none=True)

    # For AssistantMessage, preserve ToolCallItem objects
    if isinstance(msg, AssistantMessage) and msg.tool_calls is not None:
        result["tool_calls"] = msg.tool_calls  # Keep as ToolCallItem objects

    return result


# ast-grep-ignore: no-dict-any - Generic JSON serialization function
def message_to_json_dict(msg: LLMMessage | dict[str, Any]) -> dict[str, Any]:
    """
    Convert a message to a fully serialized dictionary suitable for JSON encoding.

    Unlike message_to_dict(), this function recursively serializes all nested objects
    including ToolCallItem objects, making the result safe for json.dumps().

    Args:
        msg: The message to convert (can be a Pydantic message or dict)

    Returns:
        Fully serialized dictionary safe for JSON encoding
    """
    if isinstance(msg, dict):
        return msg

    # Use model_dump to get fully serialized dict (no nested Pydantic objects)
    return msg.model_dump(mode="python", exclude_none=True)


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
) -> ToolMessage:
    """
    Convert a ToolResult to a ToolMessage for LLM consumption.

    This includes the _attachments field for provider handling.

    Args:
        result: The ToolResult to convert
        tool_call_id: The tool call ID
        function_name: The function name

    Returns:
        ToolMessage with attachments for provider processing
    """
    # Prepare attachment metadata for database storage
    attachments_metadata = None
    if result.attachments:
        attachments_metadata = [
            {
                "type": "tool_result",
                "mime_type": att.mime_type,
                "description": att.description,
                "attachment_id": att.attachment_id,  # Include ID for references
            }
            for att in result.attachments
        ]

    return ToolMessage(
        tool_call_id=tool_call_id,
        content=result.get_text(),  # Use fallback mechanism
        name=function_name,
        tool_result=result,  # Preserve original ToolResult for debugging
        _attachments=result.attachments,  # Pass attachments to provider (using alias)
        attachments=attachments_metadata,  # Store metadata in database
    )
