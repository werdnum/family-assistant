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
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field  # Added asdict
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

import aiofiles  # type: ignore[import-untyped] # For async file operations
import litellm  # Import litellm
from litellm import acompletion
from litellm.exceptions import (
    APIConnectionError,
    APIError,
    BadRequestError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)

from .factory import LLMClientFactory

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


class BaseLLMClient:
    """Base class providing common functionality for LLM clients"""

    def _supports_multimodal_tools(self) -> bool:
        """Check if this LLM client supports multimodal tool responses natively"""
        # Default implementation - override in subclasses
        return False

    def _create_attachment_injection(
        self,
        attachment: "ToolAttachment",
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    ) -> dict[str, Any]:
        """Create a message to inject attachment content after tool response

        Override in subclasses to handle provider-specific formats.
        """
        content = (
            f"[System: File from previous tool response - {attachment.description}]"
        )
        if attachment.attachment_id:
            content += f"\n[Attachment ID: {attachment.attachment_id}]"
        return {
            "role": "user",
            "content": content,
        }

    def _process_tool_messages(
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        self,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        messages: list[dict[str, Any]],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    ) -> list[dict[str, Any]]:
        """Process messages, handling tool attachments"""
        processed = []
        pending_attachments: list[ToolAttachment] = []

        for original_msg in messages:
            msg = original_msg.copy()  # Create copy to avoid side effects
            if msg.get("role") == "tool" and msg.get("_attachments"):
                # Store attachments for injection (they already have attachment_id populated)
                pending_attachments = msg.pop("_attachments")
                if pending_attachments:
                    # Use singular form for single attachment, plural for multiple
                    if len(pending_attachments) == 1:
                        msg["content"] = (
                            msg.get("content", "")
                            + "\n[File content in following message]"
                        )
                    else:
                        msg["content"] = (
                            msg.get("content", "")
                            + f"\n[{len(pending_attachments)} file(s) content in following message(s)]"
                        )

            processed.append(msg)

            # Inject attachments after tool message if needed
            if pending_attachments and not self._supports_multimodal_tools():
                for attachment in pending_attachments:
                    injection_msg = self._create_attachment_injection(attachment)
                    processed.append(injection_msg)
                pending_attachments = []

        return processed


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


def _format_tool_calls_for_debug(tool_calls: list | None) -> str:
    """Format tool calls for debug logging."""
    if not tool_calls:
        return ""

    formatted_calls = []
    for call in tool_calls:
        if isinstance(call, dict):
            name = call.get("function", {}).get("name", call.get("name", "unknown"))
            call_id = call.get("id", "no_id")
            formatted_calls.append(f"{name}(id={call_id})")
        else:
            formatted_calls.append(str(call))

    return " + tool_call(" + ", ".join(formatted_calls) + ")"


def _format_messages_for_debug(
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    messages: list[dict[str, Any]],
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
) -> str:
    """Format messages for debug logging."""
    lines = [f"=== LLM Request ({len(messages)} messages) ==="]

    for i, msg in enumerate(messages):
        role = msg.get("role", "unknown")
        content = msg.get("content", "")

        # Check for parts field (used by some providers like Gemini)
        if "parts" in msg and not content:
            parts = msg["parts"]
            if isinstance(parts, list):
                parts_strs = []
                for part in parts:
                    if isinstance(part, dict):
                        if "text" in part:
                            parts_strs.append(_truncate_content(part["text"]))
                        elif "inline_data" in part:
                            inline = part["inline_data"]
                            mime = inline.get("mime_type", "unknown")
                            data_size = len(inline.get("data", b""))
                            parts_strs.append(
                                f"[INLINE_DATA: {mime}, {data_size} bytes]"
                            )
                        else:
                            parts_strs.append(f"[PART: {list(part.keys())}]")
                    else:
                        # types.Part object or other
                        parts_strs.append(f"[{type(part).__name__}]")
                content = " + ".join(parts_strs) if parts_strs else "[empty parts]"

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
        tool_calls_str = _format_tool_calls_for_debug(msg.get("tool_calls"))

        # Format tool call info for tool role
        tool_info = ""
        if role == "tool":
            tool_call_id = msg.get("tool_call_id", "unknown")
            name = msg.get("name", "unknown")
            tool_info = f"({name}, id={tool_call_id})"

        # Check for _attachments field
        attachment_info = ""
        if "_attachments" in msg:
            attachments = msg["_attachments"]
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

    # Add tools information
    if tools:
        tool_names = [tool.get("function", {}).get("name", "unknown") for tool in tools]
        lines.append(f"Tools: {', '.join(tool_names)} ({len(tools)} available)")
    else:
        lines.append("Tools: none")

    if tool_choice:
        lines.append(f"Tool choice: {tool_choice}")

    lines.append("=" * 50)

    return "\n".join(lines)


@dataclass(frozen=True)
class ToolCallFunction:
    """Represents the function to be called in a tool call."""

    name: str
    arguments: str  # JSON string of arguments


@dataclass(frozen=True)
class ToolCallItem:
    """Represents a single tool call requested by the LLM."""

    id: str
    type: str  # Usually "function"
    function: ToolCallFunction
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    provider_metadata: dict[str, Any] | None = (
        None  # Provider-specific metadata (e.g., thought signatures)
    )


@dataclass
class LLMOutput:
    """Standardized output structure from an LLM call."""

    content: str | None = None
    tool_calls: list[ToolCallItem] | None = field(default=None)
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    reasoning_info: dict[str, Any] | None = field(
        default=None
    )  # Store reasoning/usage data
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    provider_metadata: dict[str, Any] | None = field(
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


# ast-grep-ignore: no-dict-any - Legacy code - needs structured types
# ast-grep-ignore: no-dict-any - Legacy code - needs structured types
def _sanitize_tools_for_litellm(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
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

    return sanitized_tools


class LLMInterface(Protocol):
    """Protocol defining the interface for interacting with an LLM."""

    async def generate_response(
        self,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        messages: list[dict[str, Any]],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """
        Generates a response from the LLM based on a pre-structured list of messages.
        (Existing method for direct message-based interaction)
        """
        ...

    def generate_response_stream(
        self,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        messages: list[dict[str, Any]],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, Any]] | None = None,
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


class LiteLLMClient(BaseLLMClient):
    """LLM client implementation using the LiteLLM library."""

    def _create_attachment_injection(
        self,
        attachment: "ToolAttachment",
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    ) -> dict[str, Any]:
        """Create user injection messages for attachments when using LiteLLM."""

        base_message = super()._create_attachment_injection(attachment)

        # If there's no inline content available, fall back to base class behaviour
        if attachment.content is None:
            return base_message

        base_text = str(base_message.get("content", ""))
        injection_parts: list[dict[str, Any]] = [
            {"type": "text", "text": base_text}
        ]

        if attachment.mime_type.startswith("image/"):
            image_b64 = attachment.get_content_as_base64()
            if image_b64:
                injection_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{attachment.mime_type};base64,{image_b64}"
                        },
                    }
                )
            else:
                logger.warning(
                    "LiteLLM attachment injection: Unable to encode image attachment. "
                    "Falling back to text-only description."
                )
        elif attachment.mime_type == "application/pdf":
            size_kb = len(attachment.content) / 1024
            description = attachment.description or "PDF document"
            injection_parts.append(
                {
                    "type": "text",
                    "text": (
                        f"[PDF Document: {description} ({size_kb:.1f} KB). "
                        "Inline PDF previews are not supported via LiteLLM; "
                        "use the attachment metadata or provide a summary.]"
                    ),
                }
            )
        else:
            size_kb = len(attachment.content) / 1024
            description = attachment.description or attachment.mime_type
            injection_parts.append(
                {
                    "type": "text",
                    "text": (
                        f"[File content available: {description} "
                        f"({attachment.mime_type}, {size_kb:.1f} KB). "
                        "Inline preview not supported; refer to attachment metadata.]"
                    ),
                }
            )

        return {"role": "user", "content": injection_parts}

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
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        self,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        messages: list[dict[str, Any]],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    ) -> list[dict[str, Any]]:
        """Process messages, using native support when available"""
        if not self._supports_multimodal_tools():
            return super()._process_tool_messages(messages)

        # Claude supports multimodal natively
        processed = []
        for msg in messages:
            if msg.get("role") == "tool" and msg.get("_attachments"):
                attachments = msg.pop("_attachments")
                # Convert to Claude's format
                content = [
                    {"type": "text", "text": msg.get("content", "")},
                ]
                injection_msgs = []
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
                        injection_msgs.append(
                            self._create_attachment_injection(attachment)
                        )
                msg["content"] = content
                processed.append(msg)
                # Add injection messages after the tool message if needed
                if injection_msgs:
                    processed.extend(injection_msgs)
            else:
                processed.append(msg)
        return processed

    async def _attempt_completion(
        self,
        model_id: str,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        messages: list[dict[str, Any]],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, Any]] | None,
        tool_choice: str | None,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        specific_model_params: dict[str, dict[str, Any]],  # Corrected type
    ) -> LLMOutput:
        """Internal method to make a single attempt at LLM completion."""
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

        # LiteLLM automatically drops unsupported parameters, so we pass them all.
        if DEBUG_LLM_MESSAGES_ENABLED:
            logger.info(
                f"LLM Request to {model_id}:\n"
                f"{_format_messages_for_debug(messages, tools, tool_choice)}"
            )

        if tools:
            sanitized_tools_arg = _sanitize_tools_for_litellm(tools)
            logger.debug(
                f"Calling LiteLLM model {model_id} with {len(messages)} messages. "
                f"Tools provided. Tool choice: {tool_choice}. Filtered params: {json.dumps(completion_params, default=str)}"
            )
            response = await acompletion(
                model=model_id,
                messages=messages,
                tools=sanitized_tools_arg,
                tool_choice=tool_choice,
                stream=False,
                **completion_params,  # type: ignore[reportArgumentType]
            )
            response = cast("ModelResponse", response)
        else:
            logger.debug(
                f"Calling LiteLLM model {model_id} with {len(messages)} messages. "
                f"No tools provided. Filtered params: {json.dumps(completion_params, default=str)}"
            )
            _response_obj = await acompletion(
                model=model_id,
                messages=messages,
                stream=False,
                **completion_params,  # type: ignore[reportArgumentType]
            )
            response = cast("ModelResponse", _response_obj)

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
        return LLMOutput(
            content=content,  # type: ignore
            tool_calls=tool_calls_list if tool_calls_list else None,
            reasoning_info=reasoning_info,
        )

    async def generate_response(
        self,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        messages: list[dict[str, Any]],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Generates a response using LiteLLM, with one retry on primary model and fallback."""
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
                messages=messages,
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
                    messages=messages,
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
        actual_fallback_model_id = self.fallback_model_id or "openai/o4-mini"
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
                    messages=messages,
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
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        messages: list[dict[str, Any]],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Generate streaming response using LiteLLM."""
        return self._generate_response_stream(messages, tools, tool_choice)

    async def _generate_response_stream(
        self,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        messages: list[dict[str, Any]],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Internal async generator for streaming responses."""
        try:
            # Use primary model for streaming
            completion_params = self.default_kwargs.copy()

            # Apply model-specific parameters
            for pattern, params in self.model_parameters.items():
                matched = False
                if pattern.endswith("-"):
                    if self.model.startswith(pattern[:-1]):
                        matched = True
                elif self.model == pattern:
                    matched = True

                if matched:
                    logger.debug(
                        f"Applying streaming parameters for model '{self.model}' using pattern '{pattern}': {params}"
                    )
                    params_to_merge = params.copy()
                    if "reasoning" in params_to_merge:
                        params_to_merge.pop("reasoning")  # Not supported in streaming
                    completion_params.update(params_to_merge)
                    break

            # Prepare streaming parameters
            stream_params = {
                "model": self.model,
                "messages": messages,
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
                    f"LLM Streaming Request to {self.model}:\n"
                    f"{_format_messages_for_debug(messages, tools, tool_choice)}"
                )

            logger.debug(
                f"Starting streaming response from LiteLLM model {self.model} "
                f"with {len(messages)} messages. Tools: {bool(tools)}"
            )

            # Make streaming API call
            stream = await acompletion(**stream_params)

            # Track current tool calls being built
            # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
            current_tool_calls: dict[int, dict[str, Any]] = {}
            chunk = None  # Initialize for pylint

            async for chunk in stream:  # type: ignore[misc]
                if not chunk or not chunk.choices:
                    continue

                delta = chunk.choices[0].delta
                if not delta:
                    continue

                # Stream content chunks
                if hasattr(delta, "content") and delta.content:
                    yield LLMStreamEvent(type="content", content=delta.content)

                # Handle tool calls
                if hasattr(delta, "tool_calls") and delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        # LiteLLM should always provide an index for tool calls
                        if not hasattr(tc_delta, "index"):
                            logger.warning(
                                "Tool call delta missing index attribute, defaulting to 0. "
                                "This may cause issues with multiple tool calls."
                            )
                            idx = 0
                        else:
                            idx = tc_delta.index

                        # Initialize new tool call
                        if idx not in current_tool_calls:
                            current_tool_calls[idx] = {
                                "id": tc_delta.id if hasattr(tc_delta, "id") else None,
                                "type": tc_delta.type
                                if hasattr(tc_delta, "type")
                                else "function",
                                "function": {"name": "", "arguments": ""},
                            }

                        # Update tool call info
                        tc_data = current_tool_calls[idx]
                        if hasattr(tc_delta, "id") and tc_delta.id:
                            tc_data["id"] = tc_delta.id
                        if hasattr(tc_delta, "type") and tc_delta.type:
                            tc_data["type"] = tc_delta.type

                        # Accumulate function info
                        if hasattr(tc_delta, "function") and tc_delta.function:
                            if (
                                hasattr(tc_delta.function, "name")
                                and tc_delta.function.name
                            ):
                                tc_data["function"]["name"] = tc_delta.function.name
                            if (
                                hasattr(tc_delta.function, "arguments")
                                and tc_delta.function.arguments
                            ):
                                tc_data["function"]["arguments"] += (
                                    tc_delta.function.arguments
                                )

                        # Continue accumulating tool call data
                        # We'll emit complete tool calls after the stream ends

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

            # Extract usage info if available
            metadata = {}
            if chunk and hasattr(chunk, "usage") and chunk.usage:
                try:
                    metadata["reasoning_info"] = chunk.usage.model_dump(mode="json")
                except Exception as e:
                    logger.warning(f"Could not serialize streaming usage data: {e}")

            # Signal completion
            yield LLMStreamEvent(type="done", metadata=metadata)

        except Exception as e:
            error_message = str(e)
            logger.error(f"LiteLLM streaming error: {e}", exc_info=True)
            yield LLMStreamEvent(
                type="error",
                error=error_message,
                metadata={"error_id": str(e.__class__.__name__)},
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
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        messages: list[dict[str, Any]],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Calls the wrapped client's standard generate_response, records, and returns."""
        # This method is for the existing generate_response interface
        input_data = {
            "method": "generate_response",
            "messages": messages,
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
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        messages: list[dict[str, Any]],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, Any]] | None = None,
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
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        messages: list[dict[str, Any]],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Plays back for the standard generate_response method."""
        current_input_args = {
            "method": "generate_response",
            "messages": messages,
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
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        messages: list[dict[str, Any]],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, Any]] | None = None,
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


# Export all public classes and interfaces
__all__ = [
    "LLMInterface",
    "LLMOutput",
    "LLMStreamEvent",
    "ToolCallFunction",
    "ToolCallItem",
    "BaseLLMClient",
    "LiteLLMClient",
    "RecordingLLMClient",
    "PlaybackLLMClient",
    "LLMClientFactory",
]
