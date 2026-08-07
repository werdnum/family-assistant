"""
Module defining the interface and implementations for interacting with Large Language Models (LLMs).
"""

import base64
import json
import logging
import os
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict, TypeVar, cast

import aiofiles  # type: ignore[import-untyped] # For async file operations
from genson import SchemaBuilder  # For JSON schema generation
from opentelemetry import trace as otel_trace
from pydantic import BaseModel, ValidationError

from family_assistant.tools.types import ToolDefinition

from .base import InvalidRequestError, StructuredOutputError
from .factory import LLMClientFactory
from .google_types import GeminiProviderMetadata
from .messages import (
    AssistantMessage,
    ErrorMessage,
    LLMMessage,
    MessageReasoningInfo,
    SystemMessage,
    TextContentPart,
    ToolMessage,
    UserMessage,
    is_turn_scaffolding,
    message_to_json_dict,
    tool_result_to_llm_message,
)
from .tool_call import ToolCallFunction, ToolCallItem

# Import for multimodal tool results
if TYPE_CHECKING:
    from family_assistant.tools.types import ToolAttachment

logger = logging.getLogger(__name__)
_otel_tracer = otel_trace.get_tracer(__name__)

# TypeVar for generic structured output support
T = TypeVar("T", bound=BaseModel)

type JsonPrimitive = str | int | float | bool | None
type JsonValue = JsonPrimitive | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


class ParsedChoice(TypedDict, total=False):
    delta: Mapping[str, object]


class ParsedSSEChunk(TypedDict, total=False):
    choices: list[ParsedChoice]


class StreamEventMetadata(TypedDict, total=False):
    """Metadata attached to LLMStreamEvent, shape varies by event type."""

    reasoning_info: MessageReasoningInfo
    provider_metadata: object
    attachment_ids: list[str]
    attachments: list[dict[str, str | int | None]]
    message: AssistantMessage
    error_id: str
    last_event_id: str
    error_type: str
    provider: str
    model: str


StreamingMetadata = StreamEventMetadata

# ast-grep-ignore: no-dict-any - Content parts are provider-specific formats (OpenAI image_url, Anthropic ImageBlockParam, etc.)
UserMessageContentPart = dict[str, Any]


class UserMessageDict(TypedDict):
    """Return type for format_user_message_with_file across all LLM providers."""

    role: Literal["user"]
    # ast-grep-ignore: no-dict-any - Content parts are provider-specific formats (OpenAI image_url, Anthropic ImageBlockParam, etc.)
    content: str | list[UserMessageContentPart]


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

        Turn scaffolding is skipped. The prompt now ends with the generated
        ``<turn_context>`` block, which is never empty, so checking the literal
        last user message would make this guard unreachable and let an empty
        trigger (a sticker, an unsupported media type) reach the provider as a
        generic 400 instead of a typed error naming the real problem.
        """
        last_user_message = None
        for msg in reversed(messages):
            if isinstance(msg, UserMessage) and not is_turn_scaffolding(msg):
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

    def _add_system_instruction(
        self,
        messages: Sequence[LLMMessage],
        instruction: str,
    ) -> list[LLMMessage]:
        """Return a message list with the instruction merged into the system prompt."""
        messages_with_instruction = list(messages)

        if messages_with_instruction and isinstance(
            messages_with_instruction[0], SystemMessage
        ):
            original_content = messages_with_instruction[0].content
            messages_with_instruction[0] = SystemMessage(
                content=f"{original_content}\n\n{instruction}"
            )
            return messages_with_instruction

        messages_with_instruction.insert(0, SystemMessage(content=instruction))
        return messages_with_instruction

    def _append_retry_feedback(
        self,
        messages: list[LLMMessage],
        raw_response: str | None,
        feedback: str,
    ) -> None:
        """Append the failed response and retry feedback to the conversation."""
        if raw_response is not None:
            messages.append(AssistantMessage(content=raw_response))
        messages.append(UserMessage(content=feedback))

    def _parse_json_object_response(self, raw_response: str) -> JsonObject:
        """Parse a top-level JSON object from raw model output."""
        parsed = json.loads(self._extract_json_from_response(raw_response))
        if not isinstance(parsed, dict):
            raise TypeError(
                f"Expected a JSON object but received {type(parsed).__name__}"
            )
        return cast("JsonObject", parsed)

    async def generate_json(
        self,
        messages: Sequence[LLMMessage],
        max_retries: int = 2,
    ) -> JsonObject:
        """
        Generate a parsed top-level JSON object response.

        This is the schemaless JSON mode. The provider is told to return a JSON
        object, but the expected keys and nesting are defined by the prompt
        rather than by an RPC-level schema.

        Args:
            messages: Conversation messages
            max_retries: Maximum number of retry attempts on validation failure

        Returns:
            Parsed JSON object response

        Raises:
            StructuredOutputError: If response cannot be parsed/validated after retries
        """
        instruction = (
            "You must respond with a valid JSON object. "
            "The required fields and structure are defined by the conversation. "
            "Respond ONLY with the JSON object, with no markdown or extra text."
        )
        messages_with_instruction = self._add_system_instruction(messages, instruction)

        last_error: Exception | None = None
        raw_response: str | None = None

        for attempt in range(max_retries + 1):
            try:
                llm_client = cast("LLMInterface", self)
                response = await llm_client.generate_response(messages_with_instruction)

                if not response.content:
                    raise ValueError("LLM returned empty response")

                raw_response = response.content
                return self._parse_json_object_response(raw_response)

            except json.JSONDecodeError as e:
                last_error = e
                logger.warning(
                    f"JSON output parsing failed (attempt {attempt + 1}/{max_retries + 1}): {e}"
                )

                if attempt < max_retries:
                    self._append_retry_feedback(
                        messages_with_instruction,
                        raw_response,
                        (
                            f"Your response was not valid JSON. Parse error: {e}\n\n"
                            "Please try again. Respond ONLY with a valid JSON object."
                        ),
                    )

            except TypeError as e:
                last_error = e
                logger.warning(
                    f"JSON output validation failed (attempt {attempt + 1}/{max_retries + 1}): {e}"
                )

                if attempt < max_retries:
                    self._append_retry_feedback(
                        messages_with_instruction,
                        raw_response,
                        (
                            f"Your response was valid JSON but not a JSON object. Error: {e}\n\n"
                            "Please try again. Respond ONLY with a valid JSON object."
                        ),
                    )

            except Exception as e:
                last_error = e
                logger.error(f"Unexpected error in JSON output generation: {e}")
                break

        raise StructuredOutputError(
            message=f"Failed to generate valid JSON output after {max_retries + 1} attempts",
            provider=getattr(self, "model", "unknown").split("/")[0],
            model=getattr(self, "model", "unknown"),
            raw_response=raw_response,
            validation_error=last_error,
        )

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
        messages_with_schema = self._add_system_instruction(
            messages, schema_instruction
        )

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
                    self._append_retry_feedback(
                        messages_with_schema,
                        raw_response,
                        (
                            f"Your response was not valid JSON matching the required schema. "
                            f"Error: {e}\n\n"
                            f"Please try again. Respond ONLY with valid JSON matching the schema."
                        ),
                    )

            except json.JSONDecodeError as e:
                last_error = e
                logger.warning(
                    f"Structured output JSON parsing failed (attempt {attempt + 1}/{max_retries + 1}): {e}"
                )

                if attempt < max_retries:
                    self._append_retry_feedback(
                        messages_with_schema,
                        raw_response,
                        (
                            f"Your response was not valid JSON. "
                            f"Parse error: {e}\n\n"
                            f"Please try again. Respond ONLY with valid JSON, no markdown or extra text."
                        ),
                    )

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
    reasoning_info: MessageReasoningInfo | None = field(default=None)
    provider_metadata: Any | None = field(default=None)


@dataclass
class LLMStreamEvent:
    """Event emitted during streaming LLM responses."""

    type: Literal[
        "content",
        "tool_call",
        "tool_result",
        "user_input",
        "error",
        "done",
        "thinking",
    ]
    """Kind of event.

    ``thinking`` carries a provider's reasoning text as it streams. It is
    deliberately distinct from ``content`` so that reasoning is never
    concatenated into the assistant's reply; consumers that do not recognise it
    ignore it, which is the current behaviour of the processing loop and the
    web/iOS transports. Reasoning state needed to *replay* a turn does not
    travel on these events -- it rides on the terminal ``done`` event's
    ``provider_metadata``, which is what gets persisted.
    """

    content: str | None = None  # For content chunks
    tool_call: ToolCallItem | None = None  # For tool calls
    tool_call_id: str | None = None  # For correlating tool results
    tool_result: str | None = None  # For tool execution results
    error: str | None = None  # For error messages
    # For correlating a ``user_input`` echo with the submission that produced it:
    # the originating interface's identifier for the steering message, when it
    # supplied one.
    input_id: str | None = None
    metadata: StreamEventMetadata | None = None


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
    ) -> UserMessageDict:
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
        max_retries: int = 2,
    ) -> T:
        """
        Generate a structured response matching the given Pydantic model.

        This method asks the LLM to generate output conforming to a specific
        schema defined by a Pydantic model. The response is automatically
        parsed and validated.

        Args:
            messages: Conversation messages
            response_model: Pydantic model class defining the expected response schema
            max_retries: Maximum number of retry attempts on validation failure

        Returns:
            Instance of response_model populated with the LLM's response

        Raises:
            StructuredOutputError: If response cannot be parsed/validated after retries
        """
        ...

    async def generate_json(
        self,
        messages: Sequence[LLMMessage],
        max_retries: int = 2,
    ) -> JsonObject:
        """
        Generate a parsed JSON object response.

        This is the schemaless JSON mode. It guarantees a top-level JSON
        object but does not take a response schema.

        Args:
            messages: Conversation messages
            max_retries: Maximum number of retry attempts on validation failure

        Returns:
            Parsed JSON object response

        Raises:
            StructuredOutputError: If response cannot be parsed/validated after retries
        """
        ...


class InteractionRecord(TypedDict):
    """A single recorded LLM interaction in JSONL format.

    Each record contains the serialized input arguments and the serialized output
    from a single LLM method call (generate_response, format_user_message_with_file,
    generate_structured, or generate_json).
    """

    input: dict[str, object]
    output: dict[str, object]


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
        messages_dict = [message_to_json_dict(msg) for msg in messages]
        input_data: dict[str, object] = {
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
            logger.exception(f"Error in RecordingLLMClient.generate_response: {e}")
            raise

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
    ) -> UserMessageDict:
        """Calls the wrapped client's format_user_message_with_file, records, and returns."""
        input_data: dict[str, object] = {
            "method": "format_user_message_with_file",
            "prompt_text": prompt_text,
            "file_path": file_path,
            "mime_type": mime_type,
            "max_text_length": max_text_length,
        }
        try:
            output_dict = await self.wrapped_client.format_user_message_with_file(
                prompt_text=prompt_text,
                file_path=file_path,
                mime_type=mime_type,
                max_text_length=max_text_length,
            )
            record: InteractionRecord = {
                "input": input_data,
                "output": dict(output_dict),
            }
            await self._write_record_to_file(record)
            return output_dict
        except Exception as e:
            logger.exception(
                f"Error in RecordingLLMClient.format_user_message_with_file: {e}"
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
        input_data: dict[str, object],
        output_data: LLMOutput,
    ) -> None:
        output_dict = asdict(output_data)
        record: InteractionRecord = {"input": input_data, "output": output_dict}
        await self._write_record_to_file(record)

    # ast-grep-ignore: no-dict-any - JSON-serializable recording payload with heterogeneous values
    async def _write_record_to_file(self, record: InteractionRecord) -> None:
        """Helper method to write a generic record to the recording file."""
        try:
            async with aiofiles.open(
                self.recording_path, mode="a", encoding="utf-8"
            ) as f:
                await f.write(
                    json.dumps(record, ensure_ascii=False, default=str) + "\n"
                )
            logger.debug(f"Recorded interaction to {self.recording_path}")
        except Exception as file_err:
            logger.exception(
                f"Failed to write interaction to recording file {self.recording_path}: {file_err}"
            )

    async def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
        max_retries: int = 2,
    ) -> T:
        """Calls the wrapped client's generate_structured, records, and returns."""
        messages_dict = [message_to_json_dict(msg) for msg in messages]
        input_data: dict[str, object] = {
            "method": "generate_structured",
            "messages": messages_dict,
            "response_model_name": response_model.__name__,
            "response_model_schema": response_model.model_json_schema(),
            "max_retries": max_retries,
        }
        try:
            output_data = await self.wrapped_client.generate_structured(
                messages=messages,
                response_model=response_model,
                max_retries=max_retries,
            )
            record: InteractionRecord = {
                "input": input_data,
                "output": {
                    "model_name": response_model.__name__,
                    "model_data": output_data.model_dump(),
                },
            }
            await self._write_record_to_file(record)
            return output_data
        except Exception as e:
            logger.exception(f"Error in RecordingLLMClient.generate_structured: {e}")
            raise

    async def generate_json(
        self,
        messages: Sequence[LLMMessage],
        max_retries: int = 2,
    ) -> JsonObject:
        """Calls the wrapped client's generate_json, records, and returns."""
        messages_dict = [message_to_json_dict(msg) for msg in messages]
        input_data: dict[str, object] = {
            "method": "generate_json",
            "messages": messages_dict,
            "max_retries": max_retries,
        }
        try:
            output_data = await self.wrapped_client.generate_json(
                messages=messages,
                max_retries=max_retries,
            )
            record: InteractionRecord = {
                "input": input_data,
                "output": {"json_data": output_data},
            }
            await self._write_record_to_file(record)
            return output_data
        except Exception as e:
            logger.exception(f"Error in RecordingLLMClient.generate_json: {e}")
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
        self.recorded_interactions: list[InteractionRecord] = []
        logger.info(
            f"PlaybackLLMClient initializing. Reading from: {self.recording_path}"
        )
        try:
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
                raise ValueError(
                    f"No valid interactions loaded from {self.recording_path}"
                )

            logger.info(
                f"PlaybackLLMClient initialized. Loaded {len(self.recorded_interactions)} interactions from: {self.recording_path}"
            )

        except FileNotFoundError:
            logger.error(f"Recording file not found: {self.recording_path}")
            raise
        except Exception as e:
            logger.exception(
                f"Failed to read or parse recording file {self.recording_path}: {e}"
            )
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
        messages_dict = [message_to_json_dict(msg) for msg in messages]
        current_input_args: dict[str, object] = {
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
    ) -> UserMessageDict:
        """Plays back for the format_user_message_with_file method."""
        current_input_args: dict[str, object] = {
            "method": "format_user_message_with_file",
            "prompt_text": prompt_text,
            "file_path": file_path,
            "mime_type": mime_type,
            "max_text_length": max_text_length,
        }
        return cast(
            "UserMessageDict", await self._find_and_playback_dict(current_input_args)
        )

    async def _find_and_playback_llm_output(
        self,
        current_input_args: dict[str, object],
    ) -> LLMOutput:
        """Helper to find and playback interactions that return LLMOutput."""
        logger.debug(
            f"PlaybackLLMClient attempting to find LLMOutput match for input args: {json.dumps(current_input_args, indent=2, default=str)[:500]}..."
        )
        for record in self.recorded_interactions:
            if self._recorded_input_matches(record.get("input"), current_input_args):
                logger.info(f"Found matching interaction in {self.recording_path}.")
                output_data = record["output"]
                if not isinstance(output_data, dict):
                    logger.error(
                        f"Recorded output for matched input is not a dict: {output_data}"
                    )
                    raise LookupError("Matched recorded output is not a dictionary.")

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
                    content=cast("str | None", output_data.get("content")),
                    tool_calls=reconstructed_tool_calls,
                    reasoning_info=cast(
                        "MessageReasoningInfo | None",
                        output_data.get("reasoning_info"),
                    ),
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
        self,
        current_input_args: dict[str, object],
    ) -> dict[str, object]:
        """Helper to find and playback interactions that return a simple dict."""
        logger.debug(
            f"PlaybackLLMClient attempting to find dict match for input args: {json.dumps(current_input_args, indent=2, default=str)[:500]}..."
        )
        for record in self.recorded_interactions:
            if self._recorded_input_matches(record.get("input"), current_input_args):
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

        yield LLMStreamEvent(
            type="done",
            metadata={"reasoning_info": response.reasoning_info}
            if response.reasoning_info
            else None,
        )

    # ast-grep-ignore: no-dict-any - input args dict has heterogeneous values (str, list, None) from VCR recording match keys
    async def _log_no_match_error(self, current_input_args: dict[str, object]) -> None:
        """Logs an error when no matching interaction is found."""
        logger.error(
            f"PlaybackLLMClient: No matching interaction found in {self.recording_path} for the provided input args."
        )
        try:
            failed_input_str = json.dumps(current_input_args, indent=2, default=str)
        except Exception:
            failed_input_str = str(current_input_args)
        logger.error(f"Failed Input Args:\n{failed_input_str}")

    def _normalize_recorded_input(
        self,
        input_args: object,
    ) -> dict[str, object] | None:
        """Normalize recorded input keys so old recordings still match defaults."""
        if not isinstance(input_args, dict):
            return None

        normalized = dict(input_args)
        if normalized.get("method") in {"generate_structured", "generate_json"}:
            normalized.setdefault("max_retries", 2)
        return normalized

    def _recorded_input_matches(
        self,
        recorded_input: object,
        current_input_args: dict[str, object],
    ) -> bool:
        """Compare current input args against a normalized recorded input."""
        normalized_recorded = self._normalize_recorded_input(recorded_input)
        normalized_current = self._normalize_recorded_input(current_input_args)
        return normalized_recorded == normalized_current

    async def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
        max_retries: int = 2,
    ) -> T:
        """Plays back for the generate_structured method."""
        messages_dict = [message_to_json_dict(msg) for msg in messages]
        current_input_args: dict[str, object] = {
            "method": "generate_structured",
            "messages": messages_dict,
            "response_model_name": response_model.__name__,
            "response_model_schema": response_model.model_json_schema(),
            "max_retries": max_retries,
        }

        logger.debug(
            f"PlaybackLLMClient attempting to find structured output match for input args: "
            f"{json.dumps(current_input_args, indent=2, default=str)[:500]}..."
        )

        for record in self.recorded_interactions:
            if self._recorded_input_matches(record.get("input"), current_input_args):
                logger.info(
                    f"Found matching structured output interaction in {self.recording_path}."
                )
                output_data = record["output"]
                if not isinstance(output_data, dict):
                    logger.error(
                        f"Recorded output for matched structured input is not a dict: {output_data}"
                    )
                    raise LookupError("Matched recorded output is not a dictionary.")

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

    async def generate_json(
        self,
        messages: Sequence[LLMMessage],
        max_retries: int = 2,
    ) -> JsonObject:
        """Plays back for the generate_json method."""
        messages_dict = [message_to_json_dict(msg) for msg in messages]
        current_input_args: dict[str, object] = {
            "method": "generate_json",
            "messages": messages_dict,
            "max_retries": max_retries,
        }

        logger.debug(
            f"PlaybackLLMClient attempting to find JSON output match for input args: "
            f"{json.dumps(current_input_args, indent=2, default=str)[:500]}..."
        )

        for record in self.recorded_interactions:
            if self._recorded_input_matches(record.get("input"), current_input_args):
                logger.info(
                    f"Found matching JSON output interaction in {self.recording_path}."
                )
                output_data = record["output"]
                if not isinstance(output_data, dict):
                    logger.error(
                        f"Recorded output for matched JSON input is not a dict: {output_data}"
                    )
                    raise LookupError("Matched recorded output is not a dictionary.")

                json_data = output_data.get("json_data")
                if json_data is None:
                    raise LookupError("Recorded JSON output missing 'json_data' field.")
                if not isinstance(json_data, dict):
                    raise LookupError("Recorded JSON output is not a JSON object.")

                return cast("JsonObject", json_data)

        await self._log_no_match_error(current_input_args)
        raise LookupError(
            f"No matching JSON output recorded interaction found in {self.recording_path}."
        )


# Export all public classes and interfaces
__all__ = [
    "AssistantMessage",
    "BaseLLMClient",
    "ErrorMessage",
    "JsonObject",
    "JsonValue",
    "LLMClientFactory",
    "LLMInterface",
    "LLMMessage",
    "LLMOutput",
    "LLMStreamEvent",
    "MessageReasoningInfo",
    "PlaybackLLMClient",
    "RecordingLLMClient",
    "StreamEventMetadata",
    "StructuredOutputError",
    "SystemMessage",
    "ToolCallFunction",
    "ToolCallItem",
    "ToolMessage",
    "UserMessage",
    "UserMessageContentPart",
    "UserMessageDict",
    "message_to_json_dict",
    "tool_result_to_llm_message",
]
