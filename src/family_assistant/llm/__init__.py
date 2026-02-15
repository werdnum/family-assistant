"""
Module defining the interface and implementations for interacting with Large Language Models (LLMs).
"""

import asyncio
import base64
import copy  # For deep copying tool definitions
import io
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field  # Added asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict, TypeVar, cast

import aiofiles  # type: ignore[import-untyped] # For async file operations
import litellm  # Import litellm
from genson import SchemaBuilder  # For JSON schema generation
from litellm import acompletion
from litellm.exceptions import (
    APIConnectionError,
    APIError,
    BadRequestError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)
from pydantic import BaseModel, ValidationError

from family_assistant.tools.types import ToolDefinition

from .base import InvalidRequestError, StructuredOutputError
from .factory import LLMClientFactory
from .google_types import GeminiProviderMetadata
from .messages import (
    AssistantMessage,
    ErrorMessage,
    LLMMessage,
    SystemMessage,
    TextContentPart,
    ToolMessage,
    UserMessage,
    message_to_json_dict,
    tool_result_to_llm_message,
)
from .request_buffer import LLMRequestRecord, get_request_buffer
from .tool_call import ToolCallFunction, ToolCallItem

# Import for multimodal tool results
if TYPE_CHECKING:
    from family_assistant.tools.types import ToolAttachment

# Removed ChatCompletionToolParam as it's causing ImportError and not explicitly used

if TYPE_CHECKING:
    from litellm import Message  # Add import for litellm.Message
    from litellm.types.files import (
        FileResponse,  # type: ignore[attr-defined] # Changed import path
    )
    from litellm.types.utils import (
        ModelResponse,  # Import ModelResponse for type hinting
    )

logger = logging.getLogger(__name__)

# TypeVar for generic structured output support
T = TypeVar("T", bound=BaseModel)


class ParsedChoice(TypedDict, total=False):
    delta: Mapping[str, object]


class ParsedSSEChunk(TypedDict, total=False):
    choices: list[ParsedChoice]


StreamingMetadata = dict[str, object]


class BaseLLMClient:
    """Base class providing common functionality for LLM clients"""

    model: str  # Subclasses must set this attribute

    def _supports_multimodal_tools(self) -> bool:
        """Check if this LLM client supports multimodal tool responses natively"""
        # Default implementation - override in subclasses
        return False

    def _validate_user_input(self, messages: Sequence[LLMMessage]) -> None:
        """
        Validate that the messages contain non-empty user input.

        Raises InvalidRequestError if the last user message is empty.
        This prevents sending empty requests to the LLM which would
        typically result in errors anyway.
        """
        # Find the last UserMessage
        last_user_message = None
        for msg in reversed(messages):
            if isinstance(msg, UserMessage):
                last_user_message = msg
                break

        if last_user_message is None:
            return

        content = last_user_message.content
        is_empty = False

        if isinstance(content, str):
            is_empty = not content.strip()
        elif isinstance(content, list):
            # Check if list is empty or contains only empty text parts
            if not content:
                is_empty = True
            else:
                # Check if all parts are empty text
                has_non_empty = False
                for part in content:
                    if isinstance(part, TextContentPart):
                        if part.text and part.text.strip():
                            has_non_empty = True
                            break
                    else:
                        # Non-text parts (images, attachments) count as content
                        has_non_empty = True
                        break
                is_empty = not has_non_empty

        if is_empty:
            raise InvalidRequestError(
                "User message cannot be empty",
                provider=getattr(self, "model", "unknown").split("/")[0],
                model=getattr(self, "model", "unknown"),
            )

    def create_attachment_injection(
        self,
        attachment: "ToolAttachment",
    ) -> "UserMessage":
        """Create a message to inject attachment content after tool response

        For JSON/text attachments:
        - Small files (≤10KiB): Inject full content inline
        - Large files (>10KiB): Inject schema + metadata for symbolic querying via jq

        Override in subclasses to handle provider-specific formats.
        """
        # Size threshold for inline vs symbolic (10KiB = 10240 bytes)
        SIZE_THRESHOLD = 10 * 1024

        # Handle JSON/text attachments intelligently
        if (
            attachment.content
            and attachment.mime_type
            and (
                attachment.mime_type in {"application/json", "text/csv"}
                or attachment.mime_type.startswith("text/")
            )
        ):
            content_size = len(attachment.content)

            if content_size <= SIZE_THRESHOLD:
                # Small file: inject full content inline
                try:
                    decoded_content = attachment.content.decode("utf-8")
                    content = "[System: File from previous tool response]\n"
                    if attachment.description:
                        content += f"[Description: {attachment.description}]\n"
                    if attachment.attachment_id:
                        content += f"[Attachment ID: {attachment.attachment_id}]\n"
                    content += f"[Content ({content_size} bytes)]:\n{decoded_content}"
                    return UserMessage(content=content)
                except UnicodeDecodeError:
                    # Fall through to default handling
                    pass
            else:
                # Large file: inject schema for symbolic querying
                try:
                    decoded_content = attachment.content.decode("utf-8")

                    # Generate schema for JSON
                    if attachment.mime_type == "application/json":
                        try:
                            json_data = json.loads(decoded_content)

                            # Use genson to generate schema
                            builder = SchemaBuilder()
                            builder.add_object(json_data)
                            schema = builder.to_json(indent=2)

                            content = "[System: Large data attachment from previous tool response]\n"
                            if attachment.description:
                                content += f"[Description: {attachment.description}]\n"
                            content += f"[Size: {content_size} bytes ({content_size / 1024:.1f} KB)]\n"
                            if attachment.attachment_id:
                                content += (
                                    f"[Attachment ID: {attachment.attachment_id}]\n"
                                )
                            content += f"\nData structure (JSON Schema):\n{schema}\n"
                            content += "\nNote: Use the 'jq' tool to query this data symbolically. "
                            content += f"Reference attachment ID {attachment.attachment_id} in tool calls."

                            return UserMessage(content=content)
                        except json.JSONDecodeError:
                            # Not valid JSON, fall through to text handling
                            pass

                    # For large CSV or other text, provide summary
                    content = "[System: Large text file from previous tool response]\n"
                    if attachment.description:
                        content += f"[Description: {attachment.description}]\n"
                    content += (
                        f"[Size: {content_size} bytes ({content_size / 1024:.1f} KB)]\n"
                    )
                    if attachment.attachment_id:
                        content += f"[Attachment ID: {attachment.attachment_id}]\n"
                    content += f"[MIME type: {attachment.mime_type}]\n"
                    content += "\nNote: Content too large for inline display. Use tools to access this data."

                    return UserMessage(content=content)
                except UnicodeDecodeError:
                    # Fall through to default handling
                    pass

        # Default handling for other attachment types
        content = (
            f"[System: File from previous tool response - {attachment.description}]"
        )
        if attachment.attachment_id:
            content += f"\n[Attachment ID: {attachment.attachment_id}]"
        return UserMessage(content=content)

    def _process_tool_messages(
        self,
        messages: list[LLMMessage],
    ) -> list[LLMMessage]:
        """Process messages, handling tool attachments"""
        processed: list[LLMMessage] = []
        pending_attachments: list[ToolAttachment] = []

        for original_msg in messages:
            msg = original_msg
            if isinstance(msg, ToolMessage) and msg.transient_attachments:
                # Store attachments for injection (they already have attachment_id populated)
                pending_attachments = msg.transient_attachments
                if pending_attachments:
                    # Use singular form for single attachment, plural for multiple
                    if len(pending_attachments) == 1:
                        updated_content = (
                            msg.content + "\n[File content in following message]"
                        )
                    else:
                        updated_content = (
                            msg.content
                            + f"\n[{len(pending_attachments)} file(s) content in following message(s)]"
                        )
                    # Create a new ToolMessage with updated content and no attachments
                    msg = msg.model_copy(
                        update={
                            "content": updated_content,
                            "transient_attachments": None,
                        }
                    )

            processed.append(msg)

            # Inject attachments after tool message if needed
            if pending_attachments and not self._supports_multimodal_tools():
                for attachment in pending_attachments:
                    injection_msg = self.create_attachment_injection(attachment)
                    # create_attachment_injection now returns proper UserMessage
                    processed.append(injection_msg)
                pending_attachments = []

        return processed

    def _extract_json_from_response(self, raw_response: str) -> str:
        """
        Extract JSON content from an LLM response.

        Handles two formats:
        - Plain JSON (response starts with { or [)
        - JSON in the first markdown code block (```json or ```)

        We intentionally don't try to find arbitrary JSON in the response -
        the LLM is prompted to return ONLY JSON, so we trust that format.
        """
        content = raw_response.strip()

        # If it looks like plain JSON, return as-is
        if content.startswith("{") or content.startswith("["):
            return content

        # Try to extract from the first code block
        code_block_pattern = r"```(?:json)?\s*\n([\s\S]*?)\n```"
        match = re.search(code_block_pattern, content)
        if match:
            return match.group(1).strip()

        # Fall back to original content (let JSON parser give the error)
        return content

    async def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
        max_retries: int = 2,
    ) -> T:
        """
        Generate a structured response matching the given Pydantic model.

        This is a fallback implementation that:
        1. Asks the LLM to generate JSON matching the schema
        2. Parses and validates the response with Pydantic
        3. Retries on validation failure with error feedback

        Subclasses can override this to use native structured output support.

        Args:
            messages: Conversation messages
            response_model: Pydantic model class defining the expected response schema
            max_retries: Maximum number of retry attempts on validation failure

        Returns:
            Instance of response_model populated with the LLM's response

        Raises:
            StructuredOutputError: If response cannot be parsed/validated after retries
        """
        # Generate JSON schema from Pydantic model
        schema = response_model.model_json_schema()

        # Build schema instruction message
        schema_instruction = (
            "You must respond with valid JSON that matches this schema:\n"
            f"```json\n{json.dumps(schema, indent=2)}\n```\n\n"
            "Respond ONLY with the JSON object, no additional text or markdown."
        )

        # Create messages with schema instruction
        messages_with_schema: list[LLMMessage] = list(messages)

        # Add schema instruction as a system message at the beginning
        # or append to existing system message
        if messages_with_schema and isinstance(messages_with_schema[0], SystemMessage):
            # Append to existing system message
            original_content = messages_with_schema[0].content
            updated_system = SystemMessage(
                content=f"{original_content}\n\n{schema_instruction}"
            )
            messages_with_schema[0] = updated_system
        else:
            # Insert new system message at the beginning
            messages_with_schema.insert(0, SystemMessage(content=schema_instruction))

        last_error: Exception | None = None
        raw_response: str | None = None

        for attempt in range(max_retries + 1):
            try:
                # Generate response - cast self to LLMInterface since BaseLLMClient
                # is always used as a mixin with classes implementing LLMInterface
                llm_client = cast("LLMInterface", self)
                response = await llm_client.generate_response(messages_with_schema)

                if not response.content:
                    raise ValueError("LLM returned empty response")

                raw_response = response.content

                # Try to extract JSON from the response
                content = self._extract_json_from_response(raw_response)

                # Parse and validate with Pydantic
                return response_model.model_validate_json(content)

            except ValidationError as e:
                last_error = e
                logger.warning(
                    f"Structured output validation failed (attempt {attempt + 1}/{max_retries + 1}): {e}"
                )

                if attempt < max_retries:
                    # Add error feedback for retry
                    error_feedback = UserMessage(
                        content=(
                            f"Your response was not valid JSON matching the required schema. "
                            f"Error: {e}\n\n"
                            f"Please try again. Respond ONLY with valid JSON matching the schema."
                        )
                    )
                    messages_with_schema.append(
                        AssistantMessage(content=raw_response or "")
                    )
                    messages_with_schema.append(error_feedback)

            except json.JSONDecodeError as e:
                last_error = e
                logger.warning(
                    f"Structured output JSON parsing failed (attempt {attempt + 1}/{max_retries + 1}): {e}"
                )

                if attempt < max_retries:
                    # Add error feedback for retry
                    error_feedback = UserMessage(
                        content=(
                            f"Your response was not valid JSON. "
                            f"Parse error: {e}\n\n"
                            f"Please try again. Respond ONLY with valid JSON, no markdown or extra text."
                        )
                    )
                    messages_with_schema.append(
                        AssistantMessage(content=raw_response or "")
                    )
                    messages_with_schema.append(error_feedback)

            except Exception as e:
                # For other errors, don't retry
                last_error = e
                logger.error(f"Unexpected error in structured output generation: {e}")
                break

        # All retries exhausted
        raise StructuredOutputError(
            message=f"Failed to generate valid structured output after {max_retries + 1} attempts",
            provider=getattr(self, "model", "unknown").split("/")[0],
            model=getattr(self, "model", "unknown"),
            raw_response=raw_response,
            validation_error=last_error,
        )


# --- Conditionally Enable LiteLLM Debug Logging ---
LITELLM_DEBUG_ENABLED = os.getenv("LITELLM_DEBUG", "false").lower() in {
    "true",
    "1",
    "yes",
}
if LITELLM_DEBUG_ENABLED:
    litellm.set_verbose = True  # type: ignore[reportPrivateImportUsage]
    logger.info(
        "Enabled LiteLLM verbose logging (set_verbose = True) because LITELLM_DEBUG is set."
    )
# --- End Debug Logging Control ---

# --- Debug LLM Messages Control ---
DEBUG_LLM_MESSAGES_ENABLED = os.getenv("DEBUG_LLM_MESSAGES", "false").lower() in {
    "true",
    "1",
    "yes",
}
if DEBUG_LLM_MESSAGES_ENABLED:
    logger.info("Debug LLM messages logging is enabled (DEBUG_LLM_MESSAGES is set).")


def _truncate_content(content: str, max_length: int = 500) -> str:
    """Truncate content for debug logging, preserving readability."""
    if len(content) <= max_length:
        return content

    # Check if it's base64 data (common for images)
    if content.startswith("data:") and ";base64," in content:
        # Extract the data type and estimate size
        parts = content.split(";base64,")
        if len(parts) == 2:
            data_type = parts[0]
            b64_data = parts[1]
            try:
                decoded_size = len(base64.b64decode(b64_data, validate=True))
                return f"[BASE64_DATA: {data_type}, {decoded_size} bytes]"
            except Exception:
                return f"[BASE64_DATA: {data_type}, invalid encoding]"

    # For regular text, truncate with indication
    return content[:max_length] + f"...[truncated {len(content) - max_length} chars]"


def _format_tool_calls_for_debug(tool_calls: Sequence[ToolCallItem] | None) -> str:
    """Format typed tool calls for debug logging."""
    if not tool_calls:
        return ""

    formatted_calls = []
    for call in tool_calls:
        ts_len = 0
        if (
            isinstance(call.provider_metadata, GeminiProviderMetadata)
            and call.provider_metadata.thought_signature
        ):
            ts_len = len(call.provider_metadata.thought_signature.to_google_format())
        formatted_calls.append(f"{call.function.name}(id={call.id}, ts_len={ts_len})")
    return " + tool_call(" + ", ".join(formatted_calls) + ")"


def _format_messages_for_debug(
    messages: Sequence[LLMMessage],
    tools: Sequence[ToolDefinition] | None = None,
    tool_choice: str | None = None,
) -> str:
    """Format typed messages for debug logging."""
    lines = [f"=== LLM Request ({len(messages)} messages) ==="]

    for i, msg in enumerate(messages):
        role = msg.role
        content = msg.content

        parts_field = msg.parts if isinstance(msg, UserMessage) else None
        if parts_field and not content:
            part_descriptions: list[str] = []
            for part in parts_field:
                if isinstance(part, dict) and "text" in part:
                    part_descriptions.append(_truncate_content(str(part["text"])))
                elif isinstance(part, dict) and "inline_data" in part:
                    inline = part["inline_data"]
                    mime = inline.get("mime_type", "unknown")
                    data_size = len(inline.get("data", b""))
                    part_descriptions.append(
                        f"[INLINE_DATA: {mime}, {data_size} bytes]"
                    )
                else:
                    part_descriptions.append(f"[{type(part).__name__}]")
            content = (
                " + ".join(part_descriptions) if part_descriptions else "[empty parts]"
            )

        # Handle different content types
        if isinstance(content, list):
            # Multi-part content (text + images)
            content_parts = []
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        text = part.get("text", "")
                        content_parts.append(_truncate_content(text))
                    elif part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        content_parts.append(_truncate_content(url))
                    else:
                        content_parts.append(f"[{part.get('type', 'unknown')}]")
                else:
                    content_parts.append(_truncate_content(str(part)))
            content_str = " + ".join(content_parts)
        else:
            content_str = _truncate_content(str(content))

        # Format tool calls if present
        tool_calls = msg.tool_calls if isinstance(msg, AssistantMessage) else None
        tool_calls_str = _format_tool_calls_for_debug(tool_calls)

        # Format tool call info for tool role
        tool_info = ""
        if role == "tool" and isinstance(msg, ToolMessage):
            tool_info = f"({msg.name}, id={msg.tool_call_id})"

        # Check for transient_attachments field (for typed messages)
        attachment_info = ""
        attachments = None
        if isinstance(msg, ToolMessage) and hasattr(msg, "transient_attachments"):
            attachments = msg.transient_attachments
            if attachments:
                att_count = len(attachments)
                if hasattr(attachments[0], "mime_type"):
                    att_types = [att.mime_type for att in attachments]
                    att_sizes = [
                        len(att.content) if att.content else 0 for att in attachments
                    ]
                    attachment_info = f" [_attachments: {att_count} files - {', '.join(f'{t} ({s}b)' for t, s in zip(att_types, att_sizes, strict=True))}]"
                else:
                    attachment_info = (
                        f" [_attachments: {att_count} {type(attachments[0]).__name__}]"
                    )

        # Build the line
        line = f'  [{i}] {role}{tool_info}: "{content_str}"{tool_calls_str}{attachment_info}'
        lines.append(line)

    # Include tool info header if provided
    if tools:
        lines.append("\nTools:")
        for tool in tools:
            lines.append(f"  - {tool}")
    if tool_choice:
        lines.append(f"Tool choice: {tool_choice}")

    return "\n".join(lines)


def _format_serialized_messages_for_debug(
    messages: Sequence[dict[str, object]],
    tools: Sequence[ToolDefinition] | None = None,
    tool_choice: str | None = None,
) -> str:
    """Format already-serialized messages (dict form) for debug logging."""
    lines = [f"=== LLM Request ({len(messages)} messages) ==="]

    for i, msg in enumerate(messages):
        role = str(msg.get("role", "unknown"))
        content = msg.get("content", "")

        parts_field = msg.get("parts")
        if parts_field and not content and isinstance(parts_field, list):
            parts_strs = []
            for part in parts_field:
                if isinstance(part, dict):
                    if "text" in part:
                        parts_strs.append(_truncate_content(str(part["text"])))
                    elif "inline_data" in part:
                        inline = part["inline_data"]
                        if isinstance(inline, dict):
                            mime = str(inline.get("mime_type", "unknown"))
                            data_size = len(inline.get("data", b""))
                        else:
                            mime = "unknown"
                            data_size = 0
                        parts_strs.append(f"[INLINE_DATA: {mime}, {data_size} bytes]")
                    else:
                        parts_strs.append(f"[PART: {list(part.keys())}]")
                else:
                    parts_strs.append(f"[{type(part).__name__}]")
            content = " + ".join(parts_strs) if parts_strs else "[empty parts]"

        if isinstance(content, list):
            content_parts = []
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        text = str(part.get("text", ""))
                        content_parts.append(_truncate_content(text))
                    elif part.get("type") == "image_url":
                        image_url = part.get("image_url", {})
                        url = (
                            str(image_url.get("url", ""))
                            if isinstance(image_url, dict)
                            else ""
                        )
                        content_parts.append(_truncate_content(url))
                    else:
                        content_parts.append(f"[{part.get('type', 'unknown')}]")
                else:
                    content_parts.append(_truncate_content(str(part)))
            content_str = " + ".join(content_parts)
        else:
            content_str = _truncate_content(str(content))

        tool_calls_str = ""
        tool_calls = msg.get("tool_calls") if isinstance(msg, dict) else None
        if isinstance(tool_calls, list):
            formatted_calls = []
            for call in tool_calls:
                if isinstance(call, dict):
                    name = ""
                    function_block = call.get("function")
                    if isinstance(function_block, dict):
                        name = str(function_block.get("name", ""))
                    call_id = str(call.get("id", ""))
                    formatted_calls.append(f"{name}(id={call_id})")
            if formatted_calls:
                tool_calls_str = " + tool_call(" + ", ".join(formatted_calls) + ")"

        tool_info = ""
        if role == "tool":
            tool_call_id = str(msg.get("tool_call_id", "unknown"))
            name = str(msg.get("name", "unknown"))
            tool_info = f"({name}, id={tool_call_id})"

        attachment_info = ""
        attachments = msg.get("_attachments") if isinstance(msg, dict) else None
        if isinstance(attachments, Sequence):
            attachment_info = f" [_attachments: {len(attachments)}]"
        elif attachments:
            attachment_info = " [_attachments]"

        line = f'  [{i}] {role}{tool_info}: "{content_str}"{tool_calls_str}{attachment_info}'
        lines.append(line)

    if tools:
        lines.append("\nTools:")
        for tool in tools:
            lines.append(f"  - {tool}")
    if tool_choice:
        lines.append(f"Tool choice: {tool_choice}")

    return "\n".join(lines)


@dataclass
class LLMOutput:
    """Standardized output structure from an LLM call."""

    content: str | None = None
    tool_calls: list[ToolCallItem] | None = field(default=None)
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    reasoning_info: dict[str, Any] | None = field(
        default=None
    )  # Store reasoning/usage data
    # ast-grep-ignore: no-dict-any - Accepts both dicts (for serialization) and provider metadata objects (e.g., GeminiProviderMetadata)
    provider_metadata: Any | None = field(
        default=None
    )  # Provider-specific metadata (e.g., thought signatures)


@dataclass
class LLMStreamEvent:
    """Event emitted during streaming LLM responses."""

    type: Literal["content", "tool_call", "tool_result", "error", "done"]
    content: str | None = None  # For content chunks
    tool_call: ToolCallItem | None = None  # For tool calls
    tool_call_id: str | None = None  # For correlating tool results
    tool_result: str | None = None  # For tool execution results
    error: str | None = None  # For error messages
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    metadata: dict[str, Any] | None = None  # Additional event metadata


# ast-grep-ignore: no-dict-any - Return type intentionally untyped; deep-copies and strips fields for litellm
def _sanitize_tools_for_litellm(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
    """
    Removes unsupported 'format' fields from string parameters in tool definitions
    before sending them to LiteLLM/OpenAI, which only supports 'enum' and 'date-time'.

    Args:
        tools: A list of tool definitions in OpenAI dictionary format.

    Returns:
        A new list of sanitized tool definitions.
    """
    # Create a deep copy to avoid modifying the original list in place
    sanitized_tools = copy.deepcopy(tools)

    for tool_dict in sanitized_tools:
        func_def = tool_dict.get("function", {})
        params = func_def.get("parameters", {})
        properties = params.get("properties", {})
        tool_name = func_def.get("name", "unknown_tool")  # For logging context

        if not isinstance(properties, dict):
            logger.warning(
                f"Sanitizing tool '{tool_name}': Non-dict 'properties' found. Skipping property sanitization for this tool."
            )
            continue

        props_to_delete_format = []
        for param_name, param_details in properties.items():
            if isinstance(param_details, dict):
                param_type = param_details.get("type")
                param_format = param_details.get("format")

                if (
                    param_type == "string"
                    and param_format
                    and param_format not in {"enum", "date-time"}
                ):
                    logger.warning(
                        f"Sanitizing tool '{tool_name}': Removing unsupported format '{param_format}' from string parameter '{param_name}' for LiteLLM compatibility."
                    )
                    props_to_delete_format.append(param_name)

        for param_name in props_to_delete_format:
            if (
                param_name in properties
                and isinstance(properties[param_name], dict)
                and "format" in properties[param_name]
            ):
                del properties[param_name]["format"]

    return cast("list[dict[str, Any]]", sanitized_tools)


class LLMInterface(Protocol):
    """Protocol defining the interface for interacting with an LLM."""

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """
        Generates a response from the LLM based on a pre-structured list of messages.
        (Existing method for direct message-based interaction)
        """
        ...

    def generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """
        Generates a streaming response from the LLM.

        Yields LLMStreamEvent objects as the response is generated.
        Must be implemented by all LLM clients (can use fallback implementation).

        Note: This is typed as a regular method (not async def) that returns
        AsyncIterator because it's an async generator function.
        """
        ...

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    ) -> dict[str, Any]:
        """
        Formats a user message, potentially including file content.
        The client decides how to represent the file (e.g., Gemini Files API ref, base64 data URI).

        Args:
            prompt_text: The user's textual prompt. Can be None if the primary input is a file.
            file_path: Path to the file to be processed. Can be None.
            mime_type: MIME type of the file. Required if file_path is provided.
            max_text_length: Optional maximum length for text content truncation (applies if no file or text file).

        Returns:
            A dictionary representing a single user message, e.g.,
            {"role": "user", "content": "..."} or {"role": "user", "content": [...]}.
        """
        ...

    def create_attachment_injection(
        self,
        attachment: "ToolAttachment",
    ) -> "UserMessage":
        """
        Create a message to inject attachment content into the conversation.

        This method is used to format attachments so they are visible to the LLM.
        Implementations should format the attachment appropriately for their
        specific provider (e.g., Gemini vs OpenAI message formats).

        For JSON/text attachments:
        - Small files (≤10KiB): Inject full content inline
        - Large files (>10KiB): Inject schema + metadata for symbolic querying

        Args:
            attachment: The attachment to inject

        Returns:
            A UserMessage containing the formatted attachment content
        """
        ...

    async def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
    ) -> T:
        """
        Generate a structured response matching the given Pydantic model.

        This method asks the LLM to generate output conforming to a specific
        schema defined by a Pydantic model. The response is automatically
        parsed and validated.

        Args:
            messages: Conversation messages
            response_model: Pydantic model class defining the expected response schema

        Returns:
            Instance of response_model populated with the LLM's response

        Raises:
            StructuredOutputError: If response cannot be parsed/validated after retries
        """
        ...


class LiteLLMClient(BaseLLMClient):
    """LLM client implementation using the LiteLLM library."""

    def __init__(
        self,
        model: str,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        model_parameters: dict[str, dict[str, Any]] | None = None,  # Corrected type
        fallback_model_id: str | None = None,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        fallback_model_parameters: dict[str, dict[str, Any]]
        | None = None,  # Corrected type
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        **kwargs: dict[str, Any],
    ) -> None:
        """
        Initializes the LiteLLM client.

        Args:
            model: The identifier of the primary model to use.
            model_parameters: Parameters specific to the primary model (pattern -> params_dict).
            fallback_model_id: Optional identifier for a fallback model.
            fallback_model_parameters: Optional parameters for the fallback model (pattern -> params_dict).
            **kwargs: Default keyword arguments for litellm.acompletion.
        """
        if not model:
            raise ValueError("LLM model identifier cannot be empty.")
        self.model = model
        self.default_kwargs = kwargs
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        self.model_parameters: dict[str, dict[str, Any]] = (
            model_parameters or {}
        )  # Ensure correct type for self
        self.fallback_model_id = fallback_model_id
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        self.fallback_model_parameters: dict[str, dict[str, Any]] = (
            fallback_model_parameters or {}
        )  # Ensure correct type for self
        logger.info(
            f"LiteLLMClient initialized for primary model: {self.model} "
            f"with default kwargs: {self.default_kwargs}, "
            f"model-specific parameters: {self.model_parameters}. "
            f"Fallback model: {self.fallback_model_id}, "
            f"fallback params: {self.fallback_model_parameters}"
        )

    def _supports_multimodal_tools(self) -> bool:
        """Check if model supports multimodal tool responses"""
        return self.model.startswith("claude")

    def _process_tool_messages(
        self,
        messages: list[LLMMessage],
    ) -> list[LLMMessage]:
        """Process messages, using native support when available"""
        if not self._supports_multimodal_tools():
            return super()._process_tool_messages(messages)

        # Claude supports multimodal natively
        processed: list[LLMMessage] = []
        for original_msg in messages:
            if (
                isinstance(original_msg, ToolMessage)
                and original_msg.transient_attachments
            ):
                attachments = original_msg.transient_attachments
                # Convert to Claude's format
                # ast-grep-ignore: no-dict-any - LiteLLM SDK requires dict format for message content
                content: list[dict[str, Any]] = [
                    {"type": "text", "text": original_msg.content},
                ]
                injection_msgs: list[LLMMessage] = []
                for attachment in attachments:
                    if attachment.content and attachment.mime_type.startswith("image/"):
                        # Use helper method for base64 encoding
                        b64_data = attachment.get_content_as_base64()
                        if b64_data:
                            content.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": attachment.mime_type,
                                    "data": b64_data,
                                },
                            })
                    elif (
                        attachment.content and attachment.mime_type == "application/pdf"
                    ):
                        # Claude supports PDFs via document format
                        b64_data = attachment.get_content_as_base64()
                        if b64_data:
                            content.append({
                                "type": "document",
                                "source": {
                                    "type": "base64",
                                    "media_type": attachment.mime_type,
                                    "data": b64_data,
                                },
                            })
                    elif attachment.content or attachment.file_path:
                        # Unsupported attachment type or file-path-only attachment - log warning and fall back to base class behavior
                        if attachment.content:
                            logger.warning(
                                f"Unsupported attachment type {attachment.mime_type} for Claude model, falling back to text description"
                            )
                        else:
                            logger.warning(
                                f"File-path-only attachment {attachment.file_path} for Claude model, falling back to text description"
                            )
                        # Update the text content to indicate file content follows in next message
                        content[0]["text"] += "\n[File content in following message]"
                        # Fall back to base class injection method
                        injection_msg = self.create_attachment_injection(attachment)
                        injection_msgs.append(injection_msg)
                # Create a new ToolMessage with the multimodal content and no attachments
                updated_msg = original_msg.model_copy(
                    update={
                        "content": content,
                        "transient_attachments": None,
                    }
                )
                processed.append(updated_msg)
                # Add injection messages after the tool message if needed
                if injection_msgs:
                    processed.extend(injection_msgs)
            else:
                processed.append(original_msg)
        return processed

    async def _attempt_completion(
        self,
        model_id: str,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None,
        tool_choice: str | None,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        specific_model_params: dict[str, dict[str, Any]],  # Corrected type
    ) -> LLMOutput:
        """Internal method to make a single attempt at LLM completion.

        Args:
            model_id: The model identifier to use.
            messages: List of typed LLMMessage objects.
            tools: Optional list of tool definitions.
            tool_choice: Tool choice setting.
            specific_model_params: Model-specific parameters.
        """
        # Process tool attachments before sending
        messages = self._process_tool_messages(messages)

        completion_params = self.default_kwargs.copy()

        # Find and merge model-specific parameters from config for the current model_id
        reasoning_params_config = None
        # specific_model_params is the dict of (pattern -> params_dict) for the current model type
        current_model_config_params = specific_model_params

        for (
            pattern,
            params,
        ) in current_model_config_params.items():  # params is dict[str, Any]
            matched = False
            if pattern.endswith("-"):
                if model_id.startswith(pattern[:-1]):
                    matched = True
            elif model_id == pattern:
                matched = True

            if matched:
                logger.debug(
                    f"Applying parameters for model '{model_id}' using pattern '{pattern}': {params}"
                )
                params_to_merge = params.copy()
                if "reasoning" in params_to_merge and isinstance(
                    params_to_merge["reasoning"], dict
                ):
                    reasoning_params_config = params_to_merge.pop("reasoning")
                completion_params.update(params_to_merge)
                break

        if model_id.startswith("openrouter/") and reasoning_params_config:
            completion_params["reasoning"] = reasoning_params_config
            logger.debug(
                f"Adding 'reasoning' parameter for OpenRouter model '{model_id}': {reasoning_params_config}"
            )

        # Convert to dicts for SDK/API calls (using message_to_json_dict for full serialization)
        message_dicts = [message_to_json_dict(msg) for msg in messages]

        # LiteLLM automatically drops unsupported parameters, so we pass them all.
        if DEBUG_LLM_MESSAGES_ENABLED:
            logger.info(
                f"LLM Request to {model_id}:\n"
                f"{_format_serialized_messages_for_debug(message_dicts, tools, tool_choice)}"
            )

        # Prepare for request recording
        request_id = str(uuid.uuid4())[:8]
        start_time = time.monotonic()
        request_timestamp = datetime.now(UTC)

        try:
            if tools:
                sanitized_tools_arg = _sanitize_tools_for_litellm(tools)
                logger.debug(
                    f"Calling LiteLLM model {model_id} with {len(message_dicts)} messages. "
                    f"Tools provided. Tool choice: {tool_choice}. Filtered params: {json.dumps(completion_params, default=str)}"
                )
                response = await acompletion(
                    model=model_id,
                    messages=message_dicts,
                    tools=sanitized_tools_arg,
                    tool_choice=tool_choice,
                    stream=False,
                    **completion_params,  # type: ignore[reportArgumentType]
                )
                response = cast("ModelResponse", response)
            else:
                logger.debug(
                    f"Calling LiteLLM model {model_id} with {len(message_dicts)} messages. "
                    f"No tools provided. Filtered params: {json.dumps(completion_params, default=str)}"
                )
                _response_obj = await acompletion(
                    model=model_id,
                    messages=message_dicts,
                    stream=False,
                    **completion_params,  # type: ignore[reportArgumentType]
                )
                response = cast("ModelResponse", _response_obj)
        except Exception as e:
            # Record failed request
            duration_ms = (time.monotonic() - start_time) * 1000
            try:
                get_request_buffer().add(
                    LLMRequestRecord(
                        timestamp=request_timestamp,
                        request_id=request_id,
                        model_id=model_id,
                        messages=message_dicts,
                        tools=tools,
                        tool_choice=tool_choice,
                        response=None,
                        duration_ms=duration_ms,
                        error=str(e),
                    )
                )
            except Exception as record_err:
                logger.debug(f"Failed to record LLM request error: {record_err}")
            raise

        response_message: Message | None = None
        if response.choices:
            response_message = response.choices[0].message  # type: ignore[attr-defined]

        if not response_message:
            logger.warning(
                f"LiteLLM response structure unexpected or empty for model {model_id}: {response}"
            )
            raise APIError(
                message="Received empty or unexpected response from LiteLLM.",
                llm_provider="litellm",
                model=model_id,
                status_code=500,
            )

        content = response_message.get("content")
        raw_tool_calls = response_message.get("tool_calls")
        reasoning_info = None
        if hasattr(response, "usage") and response.usage:  # type: ignore[attr-defined]
            try:
                reasoning_info = response.usage.model_dump(mode="json")  # type: ignore[attr-defined]
            except Exception as usage_err:
                logger.warning(
                    f"Could not serialize response.usage for model {model_id}: {usage_err}"
                )  # type: ignore[attr-defined]

        tool_calls_list = []
        if raw_tool_calls:
            for tc_obj in raw_tool_calls:
                func_name: str | None = None
                func_args: str | None = None
                if hasattr(tc_obj, "function") and tc_obj.function:
                    if hasattr(tc_obj.function, "name"):
                        func_name = tc_obj.function.name
                    if hasattr(tc_obj.function, "arguments"):
                        func_args = tc_obj.function.arguments
                else:
                    logger.warning(
                        f"ToolCall object for model {model_id} is missing function attribute or it's None: {tc_obj}"
                    )

                if not func_name or func_args is None:
                    logger.warning(
                        f"ToolCall's function object for model {model_id} is missing name or arguments: name='{func_name}', args_present={func_args is not None}."
                    )
                    tool_call_function = ToolCallFunction(
                        name=func_name or "malformed_function_in_llm_output",
                        arguments=func_args or "{}",
                    )
                else:
                    tool_call_function = ToolCallFunction(
                        name=func_name, arguments=func_args
                    )

                tc_id = tc_obj.id if hasattr(tc_obj, "id") else None
                tc_type = tc_obj.type if hasattr(tc_obj, "type") else None
                if not tc_id or not tc_type:
                    logger.error(
                        f"ToolCall item from LLM model {model_id} missing id ('{tc_id}') or type ('{tc_type}'). Skipping."
                    )
                    continue
                tool_calls_list.append(
                    ToolCallItem(id=tc_id, type=tc_type, function=tool_call_function)
                )

        logger.debug(
            f"LiteLLM response received from model {model_id}. Content: {bool(content)}. Tool Calls: {len(tool_calls_list)}. Reasoning: {bool(reasoning_info)}"
        )
        llm_output = LLMOutput(
            content=content,  # type: ignore
            tool_calls=tool_calls_list if tool_calls_list else None,
            reasoning_info=reasoning_info,
        )

        # Record successful request
        duration_ms = (time.monotonic() - start_time) * 1000
        try:
            get_request_buffer().add(
                LLMRequestRecord(
                    timestamp=request_timestamp,
                    request_id=request_id,
                    model_id=model_id,
                    messages=message_dicts,
                    tools=tools,
                    tool_choice=tool_choice,
                    response=asdict(llm_output),
                    duration_ms=duration_ms,
                    error=None,
                )
            )
        except Exception as record_err:
            logger.debug(f"Failed to record LLM request: {record_err}")

        return llm_output

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Generates a response using LiteLLM, with one retry on primary model and fallback."""
        # Validate user input before processing
        self._validate_user_input(messages)

        # Keep messages as typed objects throughout processing
        message_list = list(messages)

        retriable_errors = (
            APIConnectionError,
            Timeout,
            RateLimitError,
            ServiceUnavailableError,
            BadRequestError,
        )
        last_exception: Exception | None = None

        # Attempt 1: Primary model
        try:
            logger.info(f"Attempt 1: Primary model ({self.model})")
            return await self._attempt_completion(
                model_id=self.model,
                messages=message_list,
                tools=tools,
                tool_choice=tool_choice,
                specific_model_params=self.model_parameters,
            )
        except retriable_errors as e:
            logger.warning(
                f"Attempt 1 (Primary model {self.model}) failed with retriable error: {e}. Retrying primary model."
            )
            last_exception = e
        except APIError as e:  # Non-retriable APIError (but not BadRequestError)
            logger.warning(
                f"Attempt 1 (Primary model {self.model}) failed with APIError: {e}. Proceeding to fallback."
            )
            last_exception = e
        except Exception as e:
            logger.error(
                f"Attempt 1 (Primary model {self.model}) failed with unexpected error: {e}",
                exc_info=True,
            )
            last_exception = e  # Store for potential re-raise if fallback also fails or isn't attempted
            # For truly unexpected errors, we might still want to try fallback if configured.

        # Attempt 2: Retry Primary model (if Attempt 1 was a retriable error)
        if isinstance(last_exception, retriable_errors):
            try:
                logger.info(f"Attempt 2: Retrying primary model ({self.model})")
                return await self._attempt_completion(
                    model_id=self.model,
                    messages=message_list,
                    tools=tools,
                    tool_choice=tool_choice,
                    specific_model_params=self.model_parameters,
                )
            except retriable_errors as e:
                logger.warning(
                    f"Attempt 2 (Retry Primary model {self.model}) failed with retriable error: {e}. Proceeding to fallback."
                )
                last_exception = e
            except APIError as e:  # Non-retriable APIError on retry
                logger.warning(
                    f"Attempt 2 (Retry Primary model {self.model}) failed with APIError: {e}. Proceeding to fallback."
                )
                last_exception = e
            except Exception as e:
                logger.error(
                    f"Attempt 2 (Retry Primary model {self.model}) failed with unexpected error: {e}",
                    exc_info=True,
                )
                last_exception = e

        # Attempt 3: Fallback model
        actual_fallback_model_id = self.fallback_model_id or "openai/gpt-5.2"
        if actual_fallback_model_id == self.model:
            logger.warning(
                f"Fallback model '{actual_fallback_model_id}' is the same as the primary model '{self.model}'. Skipping fallback."
            )
            if last_exception:
                raise last_exception
            # This case should ideally not happen if logic is correct, means no error but no success.
            raise APIError(
                message="All attempts failed without a specific error to raise.",
                llm_provider="litellm",
                model=self.model,
                status_code=500,
            )

        if last_exception:  # Ensure we only fallback if there was a prior failure
            logger.info(f"Attempt 3: Fallback model ({actual_fallback_model_id})")
            try:
                return await self._attempt_completion(
                    model_id=actual_fallback_model_id,
                    messages=message_list,
                    tools=tools,
                    tool_choice=tool_choice,
                    specific_model_params=self.fallback_model_parameters,
                )
            except Exception as e:
                logger.error(
                    f"Attempt 3 (Fallback model {actual_fallback_model_id}) also failed: {e}",
                    exc_info=True,
                )
                # Fallthrough to raise last_exception from primary model attempts,
                # or this new one if last_exception was None (though it shouldn't be here).
                last_exception = e

        # If all attempts failed, raise the last significant exception
        if last_exception:
            logger.error(
                f"All LLM attempts failed. Raising last recorded exception: {last_exception}"
            )
            raise last_exception
        else:
            # Should not be reached if logic is correct, but as a safeguard:
            logger.error(
                "All LLM attempts failed without a specific exception captured."
            )
            raise APIError(
                message="All LLM attempts failed without a specific exception.",
                llm_provider="litellm",
                model=self.model,  # Or some generic indicator
                status_code=500,
            )

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    ) -> dict[str, Any]:
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        user_content_parts: list[dict[str, Any]] = []
        actual_prompt_text = prompt_text or "Process the provided file."

        if file_path and mime_type:
            # Attempt Gemini File API if applicable
            if self.model.startswith("gemini/"):
                try:
                    logger.info(
                        f"Attempting to upload file to Gemini: {file_path} ({mime_type})"
                    )
                    if not os.getenv("GEMINI_API_KEY"):
                        raise ValueError(
                            "GEMINI_API_KEY not found in environment for Gemini file upload."
                        )

                    async with aiofiles.open(file_path, "rb") as f_bytes_io:
                        file_bytes_content = await f_bytes_io.read()

                    loop = asyncio.get_running_loop()
                    # Use litellm.file_upload for more generic provider support
                    gemini_api_key = os.getenv("GEMINI_API_KEY")
                    if not gemini_api_key:  # Redundant check, but good practice
                        raise ValueError("GEMINI_API_KEY is required.")

                    gemini_file_obj: FileResponse = await loop.run_in_executor(
                        None,  # Default ThreadPoolExecutor
                        litellm.file_upload,  # type: ignore[attr-defined] # pylint: disable=no-member # Corrected path to file_upload
                        io.BytesIO(file_bytes_content),  # file (BinaryIO)
                        os.path.basename(file_path),  # file_name
                        "gemini",  # custom_llm_provider
                        gemini_api_key,  # api_key
                        # model argument is optional for file_upload, let gemini provider handle
                    )
                    logger.info(f"File uploaded to Gemini, ID: {gemini_file_obj.id}")
                    user_content_parts.append({
                        "type": "text",
                        "text": actual_prompt_text,
                    })
                    user_content_parts.append({
                        "type": "file",
                        "file": {
                            "file_id": gemini_file_obj.id,
                            "filename": os.path.basename(
                                file_path
                            ),  # Consistent filename
                            "format": mime_type,  # Use provided mime_type
                        },
                    })
                except Exception as e:
                    logger.error(
                        f"Failed to upload file to Gemini or construct message: {e}. Falling back to base64/text.",
                        exc_info=True,
                    )
                    user_content_parts = []  # Ensure fallback if Gemini fails

            # Fallback or non-Gemini model file handling
            if (
                not user_content_parts
            ):  # Only if Gemini part didn't populate or wasn't attempted
                if mime_type.startswith("image/"):
                    try:
                        async with aiofiles.open(file_path, "rb") as f:
                            image_bytes = await f.read()
                        encoded_image = base64.b64encode(image_bytes).decode("utf-8")
                        image_url = f"data:{mime_type};base64,{encoded_image}"
                        user_content_parts.append({
                            "type": "text",
                            "text": actual_prompt_text,
                        })
                        user_content_parts.append({
                            "type": "image_url",
                            "image_url": {"url": image_url},
                        })
                    except Exception as e:
                        logger.error(
                            f"Failed to read/encode image {file_path}: {e}",
                            exc_info=True,
                        )
                        user_content_parts.append({
                            "type": "text",
                            "text": actual_prompt_text,
                        })  # Fallback to text
                elif mime_type.startswith("text/"):
                    try:
                        async with (
                            aiofiles.open(file_path, encoding="utf-8") as f
                        ):  # Changed from "r" to "rb" for consistency, but text files should be "r"
                            file_text_content = await f.read()
                        combined_text = f"{actual_prompt_text}\n\n--- File Content ---\n{file_text_content}"
                        if max_text_length and len(combined_text) > max_text_length:
                            logger.info(
                                f"Truncating combined text from {len(combined_text)} to {max_text_length} chars."
                            )
                            combined_text = combined_text[:max_text_length]
                        user_content_parts.append({
                            "type": "text",
                            "text": combined_text,
                        })
                    except Exception as e:
                        logger.error(
                            f"Failed to read text file {file_path}: {e}", exc_info=True
                        )
                        user_content_parts.append({
                            "type": "text",
                            "text": actual_prompt_text,
                        })  # Fallback to text
                else:  # Other file types
                    logger.warning(
                        f"File type {mime_type} for {file_path} not specifically handled for image/text. "
                        "Attempting generic base64 data URI."
                    )
                    try:
                        async with aiofiles.open(file_path, "rb") as f_bytes_io:
                            file_bytes = await f_bytes_io.read()
                        encoded_file_data = base64.b64encode(file_bytes).decode("utf-8")
                        file_data_uri = f"data:{mime_type};base64,{encoded_file_data}"
                        user_content_parts.append({
                            "type": "text",
                            "text": actual_prompt_text,
                        })
                        user_content_parts.append({
                            "type": "file",
                            "file": {"file_data": file_data_uri},
                        })
                        logger.info(
                            f"Prepared generic file {file_path} as base64 data URI for LLM."
                        )
                    except Exception as e:
                        logger.error(
                            f"Failed to read/encode generic file {file_path} as base64: {e}",
                            exc_info=True,
                        )
                        user_content_parts.append({
                            "type": "text",
                            "text": actual_prompt_text,
                        })  # Fallback to text

        elif prompt_text:  # Only text prompt provided
            text_to_send = prompt_text
            if max_text_length and len(text_to_send) > max_text_length:
                logger.info(
                    f"Truncating prompt text from {len(text_to_send)} to {max_text_length} chars."
                )
                text_to_send = text_to_send[:max_text_length]
            user_content_parts.append({"type": "text", "text": text_to_send})
        else:
            logger.error(
                "format_user_message_with_file called with no file and no prompt text."
            )
            raise ValueError("Cannot format user message with no input (file or text).")

        # Determine final content structure for the user message
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        final_user_content: str | list[dict[str, Any]]
        if len(user_content_parts) == 1 and user_content_parts[0]["type"] == "text":
            final_user_content = user_content_parts[0]["text"]
        else:
            final_user_content = user_content_parts

        return {"role": "user", "content": final_user_content}

    def generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Generate streaming response using LiteLLM, with one retry on primary model and fallback."""
        # Validate user input before processing
        self._validate_user_input(messages)
        return self._generate_response_stream(messages, tools, tool_choice)

    async def _attempt_streaming_completion(
        self,
        model_id: str,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None,
        tool_choice: str | None,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        specific_model_params: dict[str, dict[str, Any]],
    ) -> AsyncIterator[LLMStreamEvent]:
        """Internal async generator for streaming responses (single attempt)."""

        def _safe_get_attr(obj: object, name: str) -> object | None:
            """Get attribute or dict key safely."""
            if isinstance(obj, dict):
                return obj.get(name)
            return getattr(obj, name, None)

        def _extract_text_from_content(content: object) -> str | None:
            """Extract concatenated text from mixed streaming payloads."""
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts: list[str] = []
                for part in content:
                    text_val: Any = None
                    if isinstance(part, dict):
                        text_val = part.get("text") or part.get("content")
                    else:
                        text_val = getattr(part, "text", None)
                        if not isinstance(text_val, str):
                            value_attr = getattr(text_val, "value", None)
                            if isinstance(value_attr, str):
                                text_val = value_attr
                        if not isinstance(text_val, str):
                            content_attr = getattr(part, "content", None)
                            if isinstance(content_attr, str):
                                text_val = content_attr
                    if isinstance(text_val, str):
                        parts.append(text_val)
                if parts:
                    return "".join(parts)
            return None

        # Process tool attachments with typed messages
        processed_messages = self._process_tool_messages(list(messages))

        # Convert to dicts only at SDK boundary
        message_dicts = [message_to_json_dict(msg) for msg in processed_messages]

        # Use default kwargs as base
        completion_params = self.default_kwargs.copy()

        # Apply model-specific parameters
        for pattern, params in specific_model_params.items():
            matched = False
            if pattern.endswith("-"):
                if model_id.startswith(pattern[:-1]):
                    matched = True
            elif model_id == pattern:
                matched = True

            if matched:
                logger.debug(
                    f"Applying streaming parameters for model '{model_id}' using pattern '{pattern}': {params}"
                )
                params_to_merge = params.copy()
                if "reasoning" in params_to_merge:
                    params_to_merge.pop("reasoning")  # Not supported in streaming
                completion_params.update(params_to_merge)
                break

        # Prepare streaming parameters
        stream_params = {
            "model": model_id,
            "messages": message_dicts,
            "stream": True,  # Enable streaming
            **completion_params,
        }

        # Add tools if provided
        if tools:
            sanitized_tools = _sanitize_tools_for_litellm(tools)
            stream_params["tools"] = sanitized_tools
            stream_params["tool_choice"] = tool_choice

        if DEBUG_LLM_MESSAGES_ENABLED:
            logger.info(
                f"LLM Streaming Request to {model_id}:\n"
                f"{_format_serialized_messages_for_debug(message_dicts, tools, tool_choice)}"
            )

        logger.debug(
            f"Starting streaming response from LiteLLM model {model_id} "
            f"with {len(message_dicts)} messages. Tools: {bool(tools)}"
        )

        # Make streaming API call
        stream = await acompletion(**stream_params)

        # Track current tool calls being built
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        current_tool_calls: dict[int, dict[str, Any]] = {}
        chunk: Any | None = None
        last_chunk_with_usage: Any | None = None
        content_emitted = False

        async for chunk in stream:  # type: ignore[misc]
            if not chunk or not chunk.choices:
                continue

            try:
                logger.debug(
                    "Raw streaming chunk choice dump: %s",
                    chunk.choices[0].model_dump()
                    if hasattr(chunk.choices[0], "model_dump")
                    else chunk.choices[0],
                )
            except Exception:
                logger.debug("Could not dump streaming chunk choice", exc_info=True)

            delta = chunk.choices[0].delta
            if not delta:
                continue

            last_chunk_with_usage = chunk

            logger.debug("Streaming delta payload: %s (type=%s)", delta, type(delta))

            # Extract content
            delta_content = _safe_get_attr(delta, "content")
            if delta_content is None:
                logger.debug(
                    "Delta content missing; available attributes: %s",
                    {
                        attr: getattr(delta, attr)
                        for attr in dir(delta)
                        if not attr.startswith("_")
                    },
                )

            text_chunk: str | None = _extract_text_from_content(delta_content)

            # Some SDKs wrap content under delta.message["content"]
            if not text_chunk:
                message_obj = _safe_get_attr(delta, "message")
                if message_obj is not None:
                    msg_content = _safe_get_attr(message_obj, "content")
                    text_chunk = _extract_text_from_content(msg_content)

            if not text_chunk and delta_content is not None:
                # Fallback: coerce non-string content to string if present
                text_chunk = str(delta_content)

            if text_chunk:
                logger.debug("Streaming content chunk: %s", text_chunk)
                yield LLMStreamEvent(type="content", content=text_chunk)
                content_emitted = True

            # Extract tool calls
            raw_tool_calls = _safe_get_attr(delta, "tool_calls")
            tool_calls_delta = (
                raw_tool_calls if isinstance(raw_tool_calls, list) else []
            )

            for tc_delta in tool_calls_delta:
                raw_idx = _safe_get_attr(tc_delta, "index")
                if isinstance(raw_idx, int):
                    idx = raw_idx
                else:
                    logger.warning(
                        "Tool call delta missing index attribute, defaulting to 0. "
                        "This may cause issues with multiple tool calls."
                    )
                    idx = 0
                tc_id = _safe_get_attr(tc_delta, "id")
                tc_type = _safe_get_attr(tc_delta, "type") or "function"
                func_name = ""
                func_args = ""
                function_delta = _safe_get_attr(tc_delta, "function")
                if function_delta:
                    func_name_attr = _safe_get_attr(function_delta, "name")
                    func_args_attr = _safe_get_attr(function_delta, "arguments")
                    if isinstance(func_name_attr, str):
                        func_name = func_name_attr
                    if isinstance(func_args_attr, str):
                        func_args = func_args_attr
                    elif func_args_attr is not None:
                        try:
                            func_args = json.dumps(func_args_attr)
                        except Exception:  # pragma: no cover - best effort fallback
                            func_args = str(func_args_attr)

                if idx not in current_tool_calls:
                    current_tool_calls[idx] = {
                        "id": tc_id,
                        "type": tc_type,
                        "function": {"name": "", "arguments": ""},
                    }

                tc_data = current_tool_calls[idx]
                if tc_id:
                    tc_data["id"] = tc_id
                if tc_type:
                    tc_data["type"] = tc_type
                if func_name:
                    tc_data["function"]["name"] = func_name
                if func_args:
                    tc_data["function"]["arguments"] += func_args

        # Emit any remaining tool calls
        for tc_data in current_tool_calls.values():
            if tc_data["id"] and tc_data["function"]["name"]:
                tool_call = ToolCallItem(
                    id=tc_data["id"],
                    type=tc_data["type"],
                    function=ToolCallFunction(
                        name=tc_data["function"]["name"],
                        arguments=tc_data["function"]["arguments"] or "{}",
                    ),
                )
                yield LLMStreamEvent(
                    type="tool_call",
                    tool_call=tool_call,
                    tool_call_id=tc_data["id"],
                )

        # If we never emitted content (e.g., provider returned only a terminal chunk),
        # fall back to a non-streaming completion to preserve expected behaviour.
        if not content_emitted:
            try:
                fallback_output = await self._attempt_completion(
                    model_id=model_id,
                    messages=list(messages),
                    tools=tools,
                    tool_choice=tool_choice,
                    specific_model_params=specific_model_params,
                )
                if fallback_output.content:
                    yield LLMStreamEvent(
                        type="content", content=fallback_output.content
                    )
                    content_emitted = True
                if fallback_output.tool_calls:
                    for tc in fallback_output.tool_calls:
                        yield LLMStreamEvent(
                            type="tool_call",
                            tool_call=tc,
                            tool_call_id=tc.id,
                        )
                if fallback_output.reasoning_info:
                    yield LLMStreamEvent(
                        type="done",
                        metadata={"reasoning_info": fallback_output.reasoning_info},
                    )
                    return
            except Exception as exc:  # pragma: no cover - best effort fallback
                logger.debug("Fallback non-streaming completion failed: %s", exc)

        # Extract usage info if available
        metadata: StreamingMetadata = {}
        if (
            last_chunk_with_usage
            and hasattr(last_chunk_with_usage, "usage")
            and last_chunk_with_usage.usage
        ):
            try:
                metadata["reasoning_info"] = last_chunk_with_usage.usage.model_dump(
                    mode="json"
                )
            except Exception as e:
                logger.warning(
                    f"Could not serialize streaming usage data: {e}", exc_info=False
                )

        # Signal completion
        yield LLMStreamEvent(type="done", metadata=metadata)

    async def _generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Internal async generator for streaming responses with retry/fallback logic."""

        retriable_errors = (
            APIConnectionError,
            Timeout,
            RateLimitError,
            ServiceUnavailableError,
            BadRequestError,
        )
        last_exception: Exception | None = None
        has_yielded_content = False

        # Attempt 1: Primary model
        try:
            logger.info(f"Attempt 1: Primary model ({self.model}) (Streaming)")
            async for event in self._attempt_streaming_completion(
                model_id=self.model,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                specific_model_params=self.model_parameters,
            ):
                if event.type in {"content", "tool_call", "done"}:
                    has_yielded_content = True
                yield event
            return
        except retriable_errors as e:
            if has_yielded_content:
                logger.error(
                    f"Attempt 1 (Primary model {self.model}) failed mid-stream with retriable error: {e}. Cannot retry as content already yielded."
                )
                yield LLMStreamEvent(
                    type="error",
                    error=str(e),
                    metadata={"error_id": str(e.__class__.__name__)},
                )
                return
            logger.warning(
                f"Attempt 1 (Primary model {self.model}) failed with retriable error: {e}. Retrying primary model."
            )
            last_exception = e
        except APIError as e:  # Non-retriable APIError (but not BadRequestError)
            if has_yielded_content:
                logger.error(
                    f"Attempt 1 (Primary model {self.model}) failed mid-stream with APIError: {e}. Cannot fallback."
                )
                yield LLMStreamEvent(
                    type="error",
                    error=str(e),
                    metadata={"error_id": str(e.__class__.__name__)},
                )
                return
            logger.warning(
                f"Attempt 1 (Primary model {self.model}) failed with APIError: {e}. Proceeding to fallback."
            )
            last_exception = e
        except Exception as e:
            if has_yielded_content:
                logger.error(
                    f"Attempt 1 (Primary model {self.model}) failed mid-stream with unexpected error: {e}. Cannot fallback."
                )
                yield LLMStreamEvent(
                    type="error",
                    error=str(e),
                    metadata={"error_id": str(e.__class__.__name__)},
                )
                return
            logger.error(
                f"Attempt 1 (Primary model {self.model}) failed with unexpected error: {e}",
                exc_info=True,
            )
            last_exception = e

        # Attempt 2: Retry Primary model (if Attempt 1 was a retriable error)
        if isinstance(last_exception, retriable_errors):
            try:
                logger.info(
                    f"Attempt 2: Retrying primary model ({self.model}) (Streaming)"
                )
                async for event in self._attempt_streaming_completion(
                    model_id=self.model,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    specific_model_params=self.model_parameters,
                ):
                    if event.type in {"content", "tool_call", "done"}:
                        has_yielded_content = True
                    yield event
                return
            except retriable_errors as e:
                if has_yielded_content:
                    logger.error(
                        "Attempt 2 (Retry Primary) failed mid-stream. Cannot fallback."
                    )
                    yield LLMStreamEvent(
                        type="error",
                        error=str(e),
                        metadata={"error_id": str(e.__class__.__name__)},
                    )
                    return
                logger.warning(
                    f"Attempt 2 (Retry Primary model {self.model}) failed with retriable error: {e}. Proceeding to fallback."
                )
                last_exception = e
            except APIError as e:
                if has_yielded_content:
                    logger.error(
                        "Attempt 2 (Retry Primary) failed mid-stream. Cannot fallback."
                    )
                    yield LLMStreamEvent(
                        type="error",
                        error=str(e),
                        metadata={"error_id": str(e.__class__.__name__)},
                    )
                    return
                logger.warning(
                    f"Attempt 2 (Retry Primary model {self.model}) failed with APIError: {e}. Proceeding to fallback."
                )
                last_exception = e
            except Exception as e:
                if has_yielded_content:
                    logger.error(
                        "Attempt 2 (Retry Primary) failed mid-stream. Cannot fallback."
                    )
                    yield LLMStreamEvent(
                        type="error",
                        error=str(e),
                        metadata={"error_id": str(e.__class__.__name__)},
                    )
                    return
                logger.error(
                    f"Attempt 2 (Retry Primary model {self.model}) failed with unexpected error: {e}",
                    exc_info=True,
                )
                last_exception = e

        # Attempt 3: Fallback model
        actual_fallback_model_id = self.fallback_model_id or "openai/gpt-5.2"
        if actual_fallback_model_id == self.model:
            logger.warning(
                f"Fallback model '{actual_fallback_model_id}' is the same as the primary model '{self.model}'. Skipping fallback."
            )
            if last_exception:
                yield LLMStreamEvent(
                    type="error",
                    error=str(last_exception),
                    metadata={"error_id": str(last_exception.__class__.__name__)},
                )
            return

        if last_exception:
            logger.info(
                f"Attempt 3: Fallback model ({actual_fallback_model_id}) (Streaming)"
            )
            try:
                async for event in self._attempt_streaming_completion(
                    model_id=actual_fallback_model_id,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    specific_model_params=self.fallback_model_parameters,
                ):
                    # No need to track content yielded here, as we are the last attempt
                    yield event
                return
            except Exception as e:
                logger.error(
                    f"Attempt 3 (Fallback model {actual_fallback_model_id}) also failed: {e}",
                    exc_info=True,
                )
                last_exception = e

        # If all attempts failed
        if last_exception:
            yield LLMStreamEvent(
                type="error",
                error=str(last_exception),
                metadata={"error_id": str(last_exception.__class__.__name__)},
            )

    async def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
        max_retries: int = 2,
    ) -> T:
        """
        Generate a structured response using LiteLLM's native response_format support.

        LiteLLM automatically translates response_format to the appropriate provider
        format (OpenAI JSON schema, Anthropic tool use, etc.).

        Args:
            messages: Conversation messages
            response_model: Pydantic model class defining the expected response schema
            max_retries: Maximum number of retry attempts on validation failure

        Returns:
            Instance of response_model populated with the LLM's response

        Raises:
            StructuredOutputError: If response cannot be parsed/validated after retries
        """
        # Convert messages to dict format for LiteLLM
        messages_list = list(messages)
        messages_list = self._process_tool_messages(messages_list)
        message_dicts = [message_to_json_dict(msg) for msg in messages_list]

        last_error: Exception | None = None
        raw_response: str | None = None

        for attempt in range(max_retries + 1):
            try:
                # Use LiteLLM's native response_format with Pydantic model
                # LiteLLM automatically handles conversion to provider-specific format
                completion_params = self.default_kwargs.copy()

                response_obj = await acompletion(
                    model=self.model,
                    messages=message_dicts,
                    response_format=response_model,
                    **completion_params,  # type: ignore[reportArgumentType] # LiteLLM accepts dynamic kwargs
                )
                response = cast("ModelResponse", response_obj)

                # Extract the response content
                # type: ignore needed - LiteLLM's ModelResponse type hints don't fully reflect
                # that non-streaming responses have .message on choices
                if not response.choices or not response.choices[0].message:  # type: ignore[attr-defined]
                    raise ValueError("LLM returned empty response")

                message = response.choices[0].message  # type: ignore[attr-defined]
                content = message.content

                if not content:
                    raise ValueError("LLM returned empty content")

                raw_response = content

                # LiteLLM should return valid JSON, but we still validate with Pydantic
                return response_model.model_validate_json(content)

            except ValidationError as e:
                last_error = e
                logger.warning(
                    f"Structured output validation failed (attempt {attempt + 1}/{max_retries + 1}): {e}"
                )

                if attempt < max_retries:
                    # Add error feedback for retry
                    message_dicts.append({
                        "role": "assistant",
                        "content": raw_response or "",
                    })
                    message_dicts.append({
                        "role": "user",
                        "content": (
                            f"Your response was not valid JSON matching the required schema. "
                            f"Error: {e}\n\n"
                            f"Please try again with valid JSON."
                        ),
                    })

            except json.JSONDecodeError as e:
                last_error = e
                logger.warning(
                    f"Structured output JSON parsing failed (attempt {attempt + 1}/{max_retries + 1}): {e}"
                )

                if attempt < max_retries:
                    message_dicts.append({
                        "role": "assistant",
                        "content": raw_response or "",
                    })
                    message_dicts.append({
                        "role": "user",
                        "content": (
                            f"Your response was not valid JSON. "
                            f"Parse error: {e}\n\n"
                            f"Please respond with valid JSON only."
                        ),
                    })

            except (
                APIConnectionError,
                APIError,
                BadRequestError,
                RateLimitError,
                ServiceUnavailableError,
                Timeout,
            ) as e:
                # For provider errors, don't retry at this level
                # (the caller can use RetryingLLMClient for that)
                last_error = e
                logger.error(f"LLM provider error in structured output generation: {e}")
                break

            except Exception as e:
                last_error = e
                logger.error(f"Unexpected error in structured output generation: {e}")
                break

        # All retries exhausted
        raise StructuredOutputError(
            message=f"Failed to generate valid structured output after {max_retries + 1} attempts",
            provider=self.model.split("/")[0],
            model=self.model,
            raw_response=raw_response,
            validation_error=last_error,
        )


class RecordingLLMClient:
    """
    An LLM client wrapper that records interactions (inputs and outputs)
    to a file while proxying calls to another LLM client.
    """

    def __init__(self, wrapped_client: LLMInterface, recording_path: str) -> None:
        """
        Initializes the recording client.

        Args:
            wrapped_client: The actual LLMInterface instance to use for generation.
            recording_path: Path to the file where interactions will be recorded (JSON Lines format).
        """
        if not (
            hasattr(wrapped_client, "generate_response")
            and hasattr(wrapped_client, "generate_response_stream")
            and hasattr(wrapped_client, "format_user_message_with_file")
        ):
            raise TypeError("wrapped_client must implement the LLMInterface protocol.")
        self.wrapped_client: LLMInterface = wrapped_client
        self.recording_path = recording_path
        # Ensure directory exists (optional, depends on desired behavior)
        os.makedirs(os.path.dirname(self.recording_path), exist_ok=True)
        logger.info(
            f"RecordingLLMClient initialized. Wrapping {type(wrapped_client).__name__}. Recording to: {self.recording_path}"
        )

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Calls the wrapped client's standard generate_response, records, and returns."""
        # Convert messages to dict for recording
        messages_dict = [message_to_json_dict(msg) for msg in messages]
        input_data = {
            "method": "generate_response",
            "messages": messages_dict,
            "tools": tools,
            "tool_choice": tool_choice,
        }
        try:
            output_data = await self.wrapped_client.generate_response(
                messages=messages, tools=tools, tool_choice=tool_choice
            )
            await self._record_interaction(input_data, output_data)
            return output_data
        except Exception as e:
            logger.error(
                f"Error in RecordingLLMClient.generate_response: {e}", exc_info=True
            )
            # Optionally record the error state as well, or just re-raise
            # For now, just re-raise to ensure error propagation.
            raise

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    ) -> dict[str, Any]:
        """Calls the wrapped client's format_user_message_with_file, records, and returns."""
        input_data = {
            "method": "format_user_message_with_file",
            "prompt_text": prompt_text,
            "file_path": file_path,
            "mime_type": mime_type,
            "max_text_length": max_text_length,
        }
        try:
            # Note: output_data for this method is a dict, not LLMOutput
            output_dict = await self.wrapped_client.format_user_message_with_file(
                prompt_text=prompt_text,
                file_path=file_path,
                mime_type=mime_type,
                max_text_length=max_text_length,
            )
            # For recording, we'll adapt the _record_interaction or create a new one
            # For simplicity, let's assume _record_interaction can handle a dict as "output"
            # or we make a small adjustment. Let's record it as a simple dict.
            record = {"input": input_data, "output": output_dict}
            await self._write_record_to_file(record)
            return output_dict
        except Exception as e:
            logger.error(
                f"Error in RecordingLLMClient.format_user_message_with_file: {e}",
                exc_info=True,
            )
            raise

    async def generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Delegates streaming to wrapped client - no recording for streams yet."""
        # For now, just pass through to wrapped client
        # Future: could record stream events
        # Note: type: ignore needed due to basedpyright incorrectly flagging async generators from protocols
        # as not iterable. This is a known limitation with protocol type inference.
        async for event in self.wrapped_client.generate_response_stream(  # type: ignore[reportGeneralTypeIssues]
            messages, tools, tool_choice
        ):
            yield event

    async def _record_interaction(
        self,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        input_data: dict[str, Any],
        output_data: LLMOutput,  # This is for generate_response
    ) -> None:
        # Ensure output_data is serializable (LLMOutput should be)
        # Convert ToolCallItem objects to dicts for JSON serialization
        output_dict = asdict(output_data)
        record = {"input": input_data, "output": output_dict}
        await self._write_record_to_file(record)

    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    async def _write_record_to_file(self, record: dict[str, Any]) -> None:
        """Helper method to write a generic record to the recording file."""
        try:
            async with aiofiles.open(
                self.recording_path, mode="a", encoding="utf-8"
            ) as f:
                await f.write(
                    json.dumps(record, ensure_ascii=False, default=str) + "\n"
                )  # Added default=str
            logger.debug(f"Recorded interaction to {self.recording_path}")
        except Exception as file_err:
            logger.error(
                f"Failed to write interaction to recording file {self.recording_path}: {file_err}",
                exc_info=True,
            )

    async def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
    ) -> T:
        """Calls the wrapped client's generate_structured, records, and returns."""
        # Convert messages to dict for recording
        messages_dict = [message_to_json_dict(msg) for msg in messages]
        input_data = {
            "method": "generate_structured",
            "messages": messages_dict,
            "response_model_name": response_model.__name__,
            "response_model_schema": response_model.model_json_schema(),
        }
        try:
            output_data = await self.wrapped_client.generate_structured(
                messages=messages, response_model=response_model
            )
            # Serialize the Pydantic model to JSON for recording
            record = {
                "input": input_data,
                "output": {
                    "model_name": response_model.__name__,
                    "model_data": output_data.model_dump(),
                },
            }
            await self._write_record_to_file(record)
            return output_data
        except Exception as e:
            logger.error(
                f"Error in RecordingLLMClient.generate_structured: {e}", exc_info=True
            )
            raise


class PlaybackLLMClient:
    """
    An LLM client that plays back previously recorded interactions from a file.
    Plays back recorded interactions by matching the input arguments.
    """

    def __init__(self, recording_path: str) -> None:
        """
        Initializes the playback client by loading all recorded interactions.

        Args:
            recording_path: Path to the JSON Lines file containing recorded interactions.

        Raises:
            FileNotFoundError: If the recording file does not exist.
            ValueError: If the recording file is empty or contains invalid JSON.
        """
        self.recording_path = recording_path
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        self.recorded_interactions: list[dict[str, Any]] = []
        logger.info(
            f"PlaybackLLMClient initializing. Reading from: {self.recording_path}"
        )
        try:
            # Load all interactions into memory synchronously during init
            # For async loading, this would need to be an async factory or method
            with open(self.recording_path, encoding="utf-8") as f:
                line_num = 0
                for raw_line in f:
                    line_num += 1
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        if "input" not in record or "output" not in record:
                            logger.warning(
                                f"Skipping line {line_num} in {self.recording_path}: Missing 'input' or 'output' key."
                            )
                            continue
                        self.recorded_interactions.append(record)
                    except json.JSONDecodeError:
                        logger.warning(
                            f"Skipping invalid JSON on line {line_num} in {self.recording_path}: {line[:100]}..."
                        )
                    except Exception as parse_err:
                        logger.warning(
                            f"Error parsing record on line {line_num} in {self.recording_path}: {parse_err}"
                        )

            if not self.recorded_interactions:
                logger.warning(
                    f"Recording file {self.recording_path} is empty or contains no valid records."
                )
                # Decide whether to raise an error or allow initialization with empty list
                # Raising error is safer to prevent unexpected behavior later.
                raise ValueError(
                    f"No valid interactions loaded from {self.recording_path}"
                )

            logger.info(
                f"PlaybackLLMClient initialized. Loaded {len(self.recorded_interactions)} interactions from: {self.recording_path}"
            )

        except FileNotFoundError:
            logger.error(f"Recording file not found: {self.recording_path}")
            raise  # Re-raise FileNotFoundError
        except Exception as e:
            logger.error(
                f"Failed to read or parse recording file {self.recording_path}: {e}",
                exc_info=True,
            )
            # Wrap other errors in a ValueError for consistent init failure reporting
            raise ValueError(
                f"Failed to load recording file {self.recording_path}: {e}"
            ) from e

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Plays back for the standard generate_response method."""
        # Convert messages to dict for playback matching
        messages_dict = [message_to_json_dict(msg) for msg in messages]
        current_input_args = {
            "method": "generate_response",
            "messages": messages_dict,
            "tools": tools,
            "tool_choice": tool_choice,
        }
        return await self._find_and_playback_llm_output(current_input_args)

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    ) -> dict[str, Any]:
        """Plays back for the format_user_message_with_file method."""
        current_input_args = {
            "method": "format_user_message_with_file",
            "prompt_text": prompt_text,
            "file_path": file_path,
            "mime_type": mime_type,
            "max_text_length": max_text_length,
        }
        # This method returns a dict, not LLMOutput
        return await self._find_and_playback_dict(current_input_args)

    async def _find_and_playback_llm_output(
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        self,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        current_input_args: dict[str, Any],
    ) -> LLMOutput:
        """Helper to find and playback interactions that return LLMOutput."""
        logger.debug(
            f"PlaybackLLMClient attempting to find LLMOutput match for input args: {json.dumps(current_input_args, indent=2, default=str)[:500]}..."
        )
        for record in self.recorded_interactions:
            if record.get("input") == current_input_args:
                logger.info(f"Found matching interaction in {self.recording_path}.")
                output_data = record["output"]
                if not isinstance(output_data, dict):
                    logger.error(
                        f"Recorded output for matched input is not a dict: {output_data}"
                    )
                    raise LookupError("Matched recorded output is not a dictionary.")

                # Reconstruct ToolCallItem objects from dicts
                tool_calls_data = output_data.get("tool_calls")
                reconstructed_tool_calls: list[ToolCallItem] | None = None
                if isinstance(tool_calls_data, list):
                    reconstructed_tool_calls = []
                    for tc_dict in tool_calls_data:
                        if isinstance(tc_dict, dict):
                            func_dict = tc_dict.get("function")
                            if isinstance(func_dict, dict):
                                tool_call_function = ToolCallFunction(
                                    name=func_dict.get("name", "unknown_playback_func"),
                                    arguments=func_dict.get("arguments", "{}"),
                                )
                                reconstructed_tool_calls.append(
                                    ToolCallItem(
                                        id=tc_dict.get("id", "unknown_playback_id"),
                                        type=tc_dict.get("type", "function"),
                                        function=tool_call_function,
                                    )
                                )
                            else:
                                logger.warning(
                                    f"Skipping malformed function dict in playback: {func_dict}"
                                )
                        else:
                            logger.warning(
                                f"Skipping malformed tool_call item in playback: {tc_dict}"
                            )
                elif tool_calls_data is not None:
                    logger.warning(
                        f"Expected list for tool_calls in playback, got {type(tool_calls_data)}"
                    )

                matched_output = LLMOutput(
                    content=output_data.get("content"),
                    tool_calls=reconstructed_tool_calls,
                    reasoning_info=output_data.get("reasoning_info"),
                )
                logger.debug(
                    f"Playing back matched LLMOutput. Content: {bool(matched_output.content)}. Tool Calls: {len(matched_output.tool_calls) if matched_output.tool_calls else 0}"
                )
                return matched_output

        await self._log_no_match_error(current_input_args)
        raise LookupError(
            f"No matching LLMOutput recorded interaction found in {self.recording_path} for the current input args."
        )

    async def _find_and_playback_dict(
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        self,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        current_input_args: dict[str, Any],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    ) -> dict[str, Any]:
        """Helper to find and playback interactions that return a simple dict."""
        logger.debug(
            f"PlaybackLLMClient attempting to find dict match for input args: {json.dumps(current_input_args, indent=2, default=str)[:500]}..."
        )
        for record in self.recorded_interactions:
            if record.get("input") == current_input_args:
                logger.info(f"Found matching interaction in {self.recording_path}.")
                output_data = record["output"]
                if not isinstance(output_data, dict):
                    logger.error(
                        f"Recorded output for matched input is not a dict: {output_data}"
                    )
                    raise LookupError("Matched recorded output is not a dictionary.")
                logger.debug(f"Playing back matched dict: {output_data}")
                return output_data

        await self._log_no_match_error(current_input_args)
        raise LookupError(
            f"No matching dict recorded interaction found in {self.recording_path} for the current input args."
        )

    async def generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """
        Plays back streaming response by converting recorded non-streaming response.
        """
        # Get the recorded response
        response = await self.generate_response(messages, tools, tool_choice)

        # Convert to stream events
        if response.content:
            yield LLMStreamEvent(type="content", content=response.content)

        if response.tool_calls:
            for tool_call in response.tool_calls:
                yield LLMStreamEvent(
                    type="tool_call", tool_call=tool_call, tool_call_id=tool_call.id
                )

        yield LLMStreamEvent(type="done", metadata=response.reasoning_info)

    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    async def _log_no_match_error(self, current_input_args: dict[str, Any]) -> None:
        """Logs an error when no matching interaction is found."""
        logger.error(
            f"PlaybackLLMClient: No matching interaction found in {self.recording_path} for the provided input args."
        )
        try:
            failed_input_str = json.dumps(current_input_args, indent=2, default=str)
        except Exception:
            failed_input_str = str(current_input_args)
        logger.error(f"Failed Input Args:\n{failed_input_str}")

    async def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
    ) -> T:
        """Plays back for the generate_structured method."""
        # Convert messages to dict for playback matching
        messages_dict = [message_to_json_dict(msg) for msg in messages]
        current_input_args = {
            "method": "generate_structured",
            "messages": messages_dict,
            "response_model_name": response_model.__name__,
            "response_model_schema": response_model.model_json_schema(),
        }

        logger.debug(
            f"PlaybackLLMClient attempting to find structured output match for input args: "
            f"{json.dumps(current_input_args, indent=2, default=str)[:500]}..."
        )

        for record in self.recorded_interactions:
            if record.get("input") == current_input_args:
                logger.info(
                    f"Found matching structured output interaction in {self.recording_path}."
                )
                output_data = record["output"]
                if not isinstance(output_data, dict):
                    logger.error(
                        f"Recorded output for matched structured input is not a dict: {output_data}"
                    )
                    raise LookupError("Matched recorded output is not a dictionary.")

                # Reconstruct the Pydantic model from the recorded data
                model_data = output_data.get("model_data")
                if model_data is None:
                    raise LookupError(
                        "Recorded structured output missing 'model_data' field."
                    )

                logger.debug(
                    f"Playing back matched structured output for model {response_model.__name__}"
                )
                return response_model.model_validate(model_data)

        await self._log_no_match_error(current_input_args)
        raise LookupError(
            f"No matching structured output recorded interaction found in {self.recording_path} "
            f"for model {response_model.__name__}."
        )


# Export all public classes and interfaces
__all__ = [
    "LLMInterface",
    "LLMMessage",
    "LLMOutput",
    "LLMStreamEvent",
    "ToolCallFunction",
    "ToolCallItem",
    "BaseLLMClient",
    "LiteLLMClient",
    "RecordingLLMClient",
    "PlaybackLLMClient",
    "LLMClientFactory",
    "message_to_json_dict",
    "tool_result_to_llm_message",
    "AssistantMessage",
    "ErrorMessage",
    "SystemMessage",
    "ToolMessage",
    "UserMessage",
    "StructuredOutputError",
]
