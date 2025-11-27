"""
Direct Google Generative AI (Gemini) implementation for LLM interactions.
"""

import base64
import json
import logging
import mimetypes
import os
import uuid
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolAttachment

import aiofiles
from google import genai
from google.genai import types
from google.genai.client import DebugConfig

from family_assistant.llm import (
    BaseLLMClient,
    LLMOutput,
    LLMStreamEvent,
    ToolCallFunction,
    ToolCallItem,
    _format_messages_for_debug,
)
from family_assistant.llm.google_types import (
    GeminiProviderMetadata,
    GeminiThoughtSignature,
)
from family_assistant.llm.messages import (
    AssistantMessage,
    ImageUrlContentPart,
    LLMMessage,
    TextContentPart,
    ToolMessage,
    UserMessage,
)

from ..base import (
    AuthenticationError,
    ContextLengthError,
    InvalidRequestError,
    LLMProviderError,
    ModelNotFoundError,
    ProviderConnectionError,
    ProviderTimeoutError,
    RateLimitError,
)

logger = logging.getLogger(__name__)


def _normalize_thought_signature(raw_value: bytes | None) -> bytes | None:
    """
    Ensure thought signatures are raw bytes.

    Some SDKs/fixtures may surface base64-encoded signatures; decode them if so,
    otherwise return the original bytes unchanged.
    """
    if not raw_value:
        return None

    # Try a safe base64 decode round-trip. If the value was base64-encoded text,
    # the round-trip without padding should match. Otherwise, fall back to the
    # original bytes to avoid corrupting opaque binary signatures.
    try:
        decoded = base64.b64decode(raw_value, validate=True)
        if base64.b64encode(decoded).rstrip(b"=") == raw_value.rstrip(b"="):
            return decoded
    except Exception:  # noqa: BLE001
        pass

    return raw_value


class GoogleGenAIClient(BaseLLMClient):
    """Direct Google Generative AI implementation."""

    def __init__(
        self,
        api_key: str,
        model: str,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        model_parameters: dict[str, dict[str, object]] | None = None,
        api_base: str | None = None,
        enable_url_context: bool = False,
        enable_google_search: bool = False,
        debug_messages: bool | None = None,
        # ast-grep-ignore: no-dict-any - Test infrastructure requires dict config
        debug_config: dict[str, str | None] | None = None,
        **kwargs: Any,  # noqa: ANN401 # Accepts arbitrary Google GenAI API parameters
    ) -> None:
        """
        Initialize Google GenAI client.

        Args:
            api_key: Google API key
            model: Model identifier (e.g., "gemini-2.0-flash-001", "gemini-1.5-pro")
            model_parameters: Pattern-based parameters matching existing config format
            api_base: Optional API base URL for custom endpoints.
            enable_url_context: Enable URL understanding (supports up to 20 URLs per request)
            enable_google_search: Enable Google Search grounding for real-time information
            debug_messages: Enable detailed message logging. If None, reads from DEBUG_LLM_MESSAGES env var.
            debug_config: SDK DebugConfig dict for record/replay in tests (client_mode, replay_id, replays_directory)
            **kwargs: Default parameters for generation
        """
        # Initialize the google-genai client
        if api_base:
            # For custom endpoints, we might need additional configuration
            logger.info(f"Using custom API base: {api_base}")
            # Note: The new API might handle this differently

        # Create client with optional debug_config for record/replay in tests
        if debug_config:
            self.client = genai.Client(
                api_key=api_key, debug_config=DebugConfig(**debug_config)
            )
        else:
            self.client = genai.Client(api_key=api_key)
        # Google API requires 'models/' prefix
        self.model_name = (
            f"models/{model}" if not model.startswith("models/") else model
        )
        self.model_parameters = model_parameters or {}
        self.default_kwargs = kwargs

        # New configuration options
        self.enable_url_context = enable_url_context
        self.enable_google_search = enable_google_search

        # Debug configuration - read from env var if not explicitly set
        if debug_messages is None:
            self._debug_messages = os.getenv("DEBUG_LLM_MESSAGES", "false").lower() in {
                "true",
                "1",
                "yes",
            }
        else:
            self._debug_messages = debug_messages

        logger.info(
            f"GoogleGenAIClient initialized for model: {model} with default kwargs: {kwargs}, "
            f"model-specific parameters: {model_parameters}, "
            f"URL context: {enable_url_context}, Google Search: {enable_google_search}, "
            f"debug_messages: {self._debug_messages}"
        )

    @property
    def should_debug_messages(self) -> bool:
        """Whether to log detailed message debugging information."""
        return self._debug_messages

    async def close(self) -> None:
        """Close the client and flush any pending cassettes (for record/replay)."""
        # Close the underlying API client to flush replay cassettes
        # The _api_client may be a ReplayApiClient which has a close() method
        try:
            api_client = getattr(self.client, "_api_client", None)
            if api_client and hasattr(api_client, "close"):
                api_client.close()  # type: ignore[attr-defined]
        except Exception as e:
            logger.debug(f"Error closing API client: {e}")

    async def __aenter__(self) -> "GoogleGenAIClient":
        """Enter async context manager."""
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:  # noqa: ANN401
        """Exit async context manager and close client."""
        await self.close()

    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    def _get_model_specific_params(self, model: str) -> dict[str, object]:
        """Get parameters for a specific model based on pattern matching."""
        params = {}
        for pattern, pattern_params in self.model_parameters.items():
            if pattern in model:
                params.update(pattern_params)
                logger.debug(
                    f"Applied parameters for pattern '{pattern}': {pattern_params}"
                )
        return params

    def _supports_multimodal_tools(self) -> bool:
        """Gemini doesn't support multimodal tool responses"""
        return False

    def create_attachment_injection(
        self,
        attachment: "ToolAttachment",
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    ) -> UserMessage:
        """Create user message with attachment for Gemini"""
        # Handle JSON/text attachments using base class logic first
        if (
            attachment.content
            and attachment.mime_type
            and (
                attachment.mime_type in {"application/json", "text/csv"}
                or attachment.mime_type.startswith("text/")
            )
        ):
            # Delegate to base class for intelligent JSON/text handling
            # This returns a UserMessage object
            return super().create_attachment_injection(attachment)

        # Handle multimodal content (images/PDFs) with provider-specific format
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        parts: list[dict[str, object] | types.Part] = [
            {"text": "[System: File from previous tool response]"}
        ]

        if attachment.content and (
            attachment.mime_type.startswith("image/")
            or attachment.mime_type == "application/pdf"
        ):
            # Use the recommended types.Part.from_bytes() method for both images and PDFs
            # This is the cleanest approach that works for both content types
            media_part = types.Part.from_bytes(
                data=attachment.content, mime_type=attachment.mime_type
            )
            parts.append(media_part)
        elif attachment.content:
            # Other binary content with data - describe what we have
            size_mb = len(attachment.content) / (1024 * 1024)
            parts.append({
                "text": f"[File content: {attachment.mime_type}, {size_mb:.1f}MB - {attachment.description}. Note: Binary content not accessible to model, text extraction may be needed]"
            })
        elif attachment.file_path:
            # Try to read file content for supported types
            try:
                file_path = Path(attachment.file_path)
                if file_path.exists() and file_path.is_file():
                    # Check file size before reading (20MB limit, aligned with Gemini API)
                    MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB
                    file_size = file_path.stat().st_size

                    if file_size > MAX_FILE_SIZE:
                        size_mb = file_size / (1024 * 1024)
                        parts.append({
                            "text": f"[File: {file_path.name} ({size_mb:.1f}MB) - Too large to process "
                            f"(exceeds {MAX_FILE_SIZE // (1024 * 1024)}MB limit). "
                            f"{attachment.description or 'No description'}]"
                        })
                    else:
                        # Read file content
                        file_content = file_path.read_bytes()

                        # Infer MIME type from file extension if not provided
                        effective_mime_type = attachment.mime_type
                        if not effective_mime_type:
                            guessed_mime_type, _ = mimetypes.guess_type(str(file_path))
                            if guessed_mime_type:
                                effective_mime_type = guessed_mime_type

                        # Handle supported file types with content
                        if effective_mime_type and (
                            effective_mime_type.startswith("image/")
                            or effective_mime_type == "application/pdf"
                        ):
                            media_part = types.Part.from_bytes(
                                data=file_content, mime_type=effective_mime_type
                            )
                            parts.append(media_part)
                        else:
                            # Unsupported type - describe the file
                            size_mb = len(file_content) / (1024 * 1024)
                            parts.append({
                                "text": f"[File: {file_path.name} ({effective_mime_type or 'unknown type'}, "
                                f"{size_mb:.1f}MB) - {attachment.description or 'No description'}. "
                                f"Binary content not accessible to model]"
                            })
                else:
                    parts.append({
                        "text": f"[File: {attachment.file_path} - File not found or inaccessible]"
                    })
            except Exception as e:
                # Error reading file - fall back to description
                parts.append({
                    "text": f"[File: {attachment.file_path} - Error reading file: {str(e)}]"
                })

        # Return UserMessage with parts for provider-specific handling
        return UserMessage(
            content="[Multimodal attachment]",  # Fallback content for serialization
            parts=parts,
        )

    def _convert_messages_to_genai_format(
        self,
        messages: list[LLMMessage],
    ) -> list[types.ContentUnionDict]:
        """Convert typed LLMMessage objects to Gemini format using SDK types.

        Returns a list compatible with the SDK's ContentListUnionDict type. In practice,
        we return types.Content objects, but ContentUnionDict allows for the full
        flexibility the SDK supports (Content, ContentDict, str, images, files, parts).

        All parts use types.Part with proper snake_case field names (function_call,
        function_response, thought_signature) to ensure proper SDK validation.
        """
        # Build proper Content objects for the new API
        contents: list[types.ContentUnionDict] = []

        for _msg_idx, msg in enumerate(messages):
            role = msg.role
            content = msg.content or ""

            if role == "system":
                # System messages can be included as user messages with a prefix
                contents.append(
                    types.Content(
                        role="user",
                        parts=[types.Part(text=f"System: {content}")],
                    )
                )
            elif role == "user":
                # Check if message already has parts (e.g., from attachment injection)
                if isinstance(msg, UserMessage) and msg.parts:
                    # Parts from _inject_tool_attachments can be mix of Part objects and dicts
                    # The SDK will accept both, but we document this flexibility here
                    # This is safe because _inject_tool_attachments uses types.Part.from_bytes()
                    contents.append(types.Content(role="user", parts=msg.parts))
                # Handle both simple string content and multi-part content (text + images)
                elif isinstance(content, str):
                    # Simple text content
                    contents.append(
                        types.Content(role="user", parts=[types.Part(text=content)])
                    )
                elif isinstance(content, list):
                    # Multi-part content (e.g., text + images)
                    user_parts: list[types.Part] = []
                    for part in content:
                        if isinstance(part, TextContentPart):
                            user_parts.append(types.Part(text=part.text))
                        elif isinstance(part, ImageUrlContentPart):
                            # Extract base64 image data
                            image_url = part.image_url["url"]
                            if image_url.startswith("data:"):
                                # Parse data URL: data:image/jpeg;base64,<base64_data>
                                try:
                                    # Split on comma to separate metadata from data
                                    header, base64_data = image_url.split(",", 1)
                                    # Extract MIME type
                                    mime_type = header.split(";")[0].split(":")[1]

                                    # Decode base64 to bytes
                                    image_bytes = base64.b64decode(base64_data)

                                    # Create inline data part using SDK types
                                    user_parts.append(
                                        types.Part.from_bytes(
                                            data=image_bytes,
                                            mime_type=mime_type,
                                        )
                                    )
                                except Exception as e:
                                    logger.error(f"Failed to parse image data URL: {e}")
                                    # Skip this image part if parsing fails
                            else:
                                # Non-data URLs should already be converted by ProcessingService
                                # Log a warning if we still see them here
                                logger.warning(
                                    f"Non-data URL images not supported by Gemini API: {image_url[:50]}..."
                                )
                        elif isinstance(part, str):
                            # Fallback for string parts
                            user_parts.append(types.Part(text=part))

                    if user_parts:
                        contents.append(types.Content(role="user", parts=user_parts))
                else:
                    # Fallback for other content types - try to convert to string
                    contents.append(
                        types.Content(
                            role="user", parts=[types.Part(text=str(content))]
                        )
                    )
            elif role == "assistant":
                assistant_parts: list[types.Part] = []

                # Add text content if present
                if content and isinstance(content, str):
                    assistant_parts.append(types.Part(text=content))

                # Add tool calls if present, with thought signatures attached per-call
                if isinstance(msg, AssistantMessage) and msg.tool_calls:
                    for tc in msg.tool_calls:
                        # Tool calls must be properly typed ToolCallItem objects
                        if not isinstance(tc, ToolCallItem):
                            logger.warning(
                                f"Expected ToolCallItem, got {type(tc)}. Skipping."
                            )
                            continue

                        if tc.type != "function":
                            continue

                        func_name = tc.function.name
                        func_args_str = tc.function.arguments
                        func_id = tc.id if isinstance(tc.id, str) else None
                        thought_signature_bytes = None
                        tc_metadata = tc.provider_metadata
                        if isinstance(tc_metadata, GeminiProviderMetadata):
                            if tc_metadata.thought_signature:
                                thought_signature_bytes = _normalize_thought_signature(
                                    tc_metadata.thought_signature.to_google_format()
                                )
                        elif (
                            isinstance(tc_metadata, dict)
                            and tc_metadata.get("provider") == "google"
                            and tc_metadata.get("thought_signature")
                        ):
                            try:
                                thought_signature_bytes = _normalize_thought_signature(
                                    GeminiProviderMetadata.from_dict(
                                        dict(tc_metadata)
                                    ).thought_signature.to_google_format()  # type: ignore[union-attr]
                                )
                            except Exception as e:  # noqa: BLE001
                                logger.debug(
                                    "Failed to decode thought_signature from provider_metadata: %s",
                                    e,
                                )

                        # Convert arguments to dict for SDK while keeping raw signature untouched
                        args_dict: dict[str, object] | None = None
                        if isinstance(func_args_str, dict):
                            args_dict = func_args_str
                        elif isinstance(func_args_str, str):
                            try:
                                parsed_args = json.loads(func_args_str)
                                if isinstance(parsed_args, dict):
                                    args_dict = parsed_args
                            except json.JSONDecodeError:
                                args_dict = None

                        # Create FunctionCall Part with thought_signature if present
                        # Only include thought_signature if we actually have one
                        # (per Google docs: pass it back exactly as received, or omit if not received)
                        if thought_signature_bytes:
                            # DEBUG LOGGING
                            sig_len = len(thought_signature_bytes)
                            sig_preview = thought_signature_bytes[:10]
                            logger.warning(
                                f"DEBUG: Sending thought_signature to SDK. Len: {sig_len}, Preview: {sig_preview!r}"
                            )

                            assistant_parts.append(
                                types.Part(
                                    function_call=types.FunctionCall(
                                        name=func_name,
                                        args=args_dict,
                                        id=func_id,
                                    ),
                                    thought_signature=thought_signature_bytes,
                                )
                            )
                        else:
                            assistant_parts.append(
                                types.Part(
                                    function_call=types.FunctionCall(
                                        name=func_name,
                                        args=args_dict,
                                        id=func_id,
                                    )
                                )
                            )

                if assistant_parts:
                    contents.append(types.Content(role="model", parts=assistant_parts))
            elif role == "tool":
                # Handle tool responses
                if not isinstance(msg, ToolMessage):
                    continue

                tool_content = msg.content
                # Try to parse the content as JSON
                try:
                    response_data = (
                        json.loads(tool_content)
                        if isinstance(tool_content, str)
                        else tool_content
                    )
                except json.JSONDecodeError:
                    response_data = {"result": tool_content}

                # SDK requires response to be a dict, not a primitive value
                if not isinstance(response_data, dict):
                    response_data = {"result": response_data}

                # Create FunctionResponse using SDK types with snake_case
                part = types.Part(
                    function_response=types.FunctionResponse(
                        id=msg.tool_call_id,
                        name=msg.name,
                        response=response_data,
                    ),
                )
                contents.append(
                    types.Content(
                        role="function",
                        parts=[
                            part,
                        ],
                    )
                )

        return contents

    # ast-grep-ignore: no-dict-any - Provider tool schema mirrors OpenAI format
    def _convert_tools_to_genai_format(self, tools: list[dict[str, Any]]) -> list[Any]:
        """Convert OpenAI-style tools to Gemini format."""

        function_declarations = []

        for tool in tools:
            if tool.get("type") != "function":
                continue

            func_def = tool.get("function", {})

            # Convert OpenAI-style parameters to Google schema
            params = func_def.get("parameters", {})
            properties = params.get("properties", {})

            # Convert properties to Google format
            google_properties = {}
            for prop_name, prop_def in properties.items():
                prop_type = prop_def.get("type", "string")

                if prop_type == "array":
                    # Handle array types - need to specify items
                    items_def = prop_def.get("items", {})
                    items_type = items_def.get("type", "string").upper()

                    google_properties[prop_name] = types.Schema(
                        type=types.Type.ARRAY,
                        description=prop_def.get("description", ""),
                        items=types.Schema(
                            type=items_type,
                            description=items_def.get("description", ""),
                        ),
                    )
                else:
                    # Handle non-array types
                    schema_type = prop_type.upper()
                    google_properties[prop_name] = types.Schema(
                        type=schema_type,
                        description=prop_def.get("description", ""),
                    )

            # Create function declaration
            func_decl = types.FunctionDeclaration(
                name=func_def.get("name"),
                description=func_def.get("description", ""),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties=google_properties,
                    required=params.get("required", []),
                ),
            )
            function_declarations.append(func_decl)

        # Return a Tool with all function declarations
        if function_declarations:
            return [types.Tool(function_declarations=function_declarations)]
        return []

    def _create_grounding_tools(self) -> list[Any]:
        """Create grounding tools (URL context and Google Search) based on configuration."""
        grounding_tools = []

        # Add URL context tool if enabled
        if self.enable_url_context:
            url_context_tool = types.Tool(url_context=types.UrlContext())
            grounding_tools.append(url_context_tool)
            logger.debug("Added URL context tool")

        # Add Google Search tool if enabled
        if self.enable_google_search:
            google_search_tool = types.Tool(google_search=types.GoogleSearch())
            grounding_tools.append(google_search_tool)
            logger.debug("Added Google Search grounding tool")

        return grounding_tools

    def _prepare_all_tools(
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        self,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, Any]] | None = None,
    ) -> list[Any]:
        """Prepare all tools including function tools and grounding tools."""
        all_tools = []

        # Add function tools if provided
        if tools:
            function_tools = self._convert_tools_to_genai_format(tools)
            all_tools.extend(function_tools)

        # Add grounding tools (URL context and Google Search)
        grounding_tools = self._create_grounding_tools()
        all_tools.extend(grounding_tools)

        return all_tools

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Generate response using Google GenAI."""
        try:
            # Keep messages as typed objects for processing
            typed_messages = list(messages)

            # Debug logging if enabled
            if self.should_debug_messages:
                logger.info(
                    f"=== LLM Request to {self.model_name} ===\n"
                    f"{_format_messages_for_debug(typed_messages, tools, tool_choice)}"
                )

            # Process tool attachments with typed messages
            processed_typed_messages = self._process_tool_messages(typed_messages)

            # Convert messages to format expected by new API
            # _convert_messages_to_genai_format expects typed LLMMessage objects
            contents = self._convert_messages_to_genai_format(processed_typed_messages)

            # Debug: Log post-processed messages if enabled
            if self.should_debug_messages:
                logger.info(
                    f"=== After _process_tool_messages ({len(processed_typed_messages)} messages) ===\n"
                    f"{_format_messages_for_debug(processed_typed_messages, None, None)}"
                )
                debug_lines = ["--- Gemini SDK payload (pre-stream) ---"]
                for content in contents:
                    if not isinstance(content, types.Content):
                        continue
                    part_descriptions: list[str] = []
                    for part in getattr(content, "parts", []) or []:
                        ts = getattr(part, "thought_signature", None)
                        fc = getattr(part, "function_call", None)
                        fr = getattr(part, "function_response", None)
                        ts_len = len(ts) if isinstance(ts, (bytes, bytearray)) else 0
                        fc_name = fc.name if fc else None
                        fr_name = fr.name if fr else None
                        part_descriptions.append(
                            f"part(ts_len={ts_len}, fc={fc_name}, fr={fr_name})"
                        )
                    debug_lines.append(
                        f"{content.role}: " + ", ".join(part_descriptions)
                    )
                logger.info("\n".join(debug_lines))

            # Build generation config
            config_params = {
                **self.default_kwargs,
                **self._get_model_specific_params(self.model_name),
            }

            # Map common parameters
            generation_config = types.GenerateContentConfig()
            # Set media resolution to HIGH for all requests (affects images, PDFs, videos)
            generation_config.media_resolution = (
                types.MediaResolution.MEDIA_RESOLUTION_HIGH
            )

            if "temperature" in config_params:
                generation_config.temperature = config_params["temperature"]
            if "max_tokens" in config_params:
                generation_config.max_output_tokens = config_params["max_tokens"]
            if "top_p" in config_params:
                generation_config.top_p = config_params["top_p"]
            if "top_k" in config_params:
                generation_config.top_k = config_params["top_k"]

            # Prepare all tools (function tools + grounding tools)
            all_tools = self._prepare_all_tools(tools)

            # Add tools to config if any are available
            if all_tools:
                generation_config.tools = all_tools
                # Disable automatic function calling so we can manually handle
                # function calls and thought signatures
                generation_config.automatic_function_calling = (
                    types.AutomaticFunctionCallingConfig(disable=True)
                )

            response = await self.client.aio.models.generate_content(
                model=self.model_name,
                contents=contents,
                config=generation_config,
            )

            # Extract content from response
            content = None
            if hasattr(response, "text"):
                content = response.text
            elif hasattr(response, "candidates") and response.candidates:
                # New API structure - get text from first candidate
                candidate = response.candidates[0]
                if (
                    hasattr(candidate, "content")
                    and candidate.content
                    and hasattr(candidate.content, "parts")
                ):
                    parts = candidate.content.parts
                    # Collect text from non-thought parts only
                    if parts:
                        text_parts = []
                        for part in parts:
                            # Skip thought parts - they're for debugging only
                            is_thought = hasattr(part, "thought") and part.thought
                            if not is_thought and hasattr(part, "text") and part.text:
                                text_parts.append(part.text)
                        if text_parts:
                            content = "".join(text_parts)

            # Extract tool calls and thought signatures from response
            tool_calls = None
            thought_summaries = []  # Initialize early to avoid UnboundLocalError

            if hasattr(response, "candidates") and response.candidates:
                candidate = response.candidates[0]
                if (
                    hasattr(candidate, "content")
                    and candidate.content
                    and hasattr(candidate.content, "parts")
                    and candidate.content.parts
                ):
                    found_tool_calls = []

                    for part_index, part in enumerate(candidate.content.parts):
                        # Extract thought summary if present (readable for debugging/introspection)
                        if hasattr(part, "thought") and part.thought:
                            # When part.thought is True, the thought text is in part.text
                            thought_text = getattr(part, "text", "")
                            thought_summaries.append({
                                "part_index": part_index,
                                "summary": thought_text,
                            })

                        if hasattr(part, "function_call") and part.function_call:
                            # Convert Google function call to our format
                            func_call = part.function_call
                            if not func_call.name:
                                logger.warning(
                                    "Received a tool call without a name: %s", func_call
                                )
                                continue

                            # Extract thought signature for THIS specific part if present
                            thought_sig = None
                            if (
                                hasattr(part, "thought_signature")
                                and part.thought_signature
                            ):
                                # Wrap in opaque GeminiThoughtSignature - no processing
                                thought_sig = GeminiThoughtSignature(
                                    part.thought_signature
                                )

                            # Store func_call with its thought signature
                            found_tool_calls.append((func_call, thought_sig))

                    # Create ToolCallItem objects, each with its own thought signature if present
                    if found_tool_calls:
                        tool_calls = []
                        for func_call, thought_sig in found_tool_calls:
                            provider_metadata = None
                            if thought_sig:
                                # Create GeminiProviderMetadata object with thought signature
                                provider_metadata = GeminiProviderMetadata(
                                    thought_signature=thought_sig
                                )

                            # Preserve the original args structure as provided by the SDK
                            args_value = (
                                func_call.args if func_call.args is not None else {}
                            )
                            func_call_id = (
                                func_call.id if isinstance(func_call.id, str) else None
                            )
                            call_id = func_call_id or f"call_{uuid.uuid4().hex[:24]}"

                            tool_calls.append(
                                ToolCallItem(
                                    id=call_id,
                                    type="function",
                                    function=ToolCallFunction(
                                        name=func_call.name,
                                        arguments=args_value,
                                    ),
                                    provider_metadata=provider_metadata,
                                )
                            )

            # Extract usage information if available
            reasoning_info = None
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                usage = response.usage_metadata
                reasoning_info = {
                    "prompt_tokens": getattr(usage, "prompt_token_count", 0),
                    "completion_tokens": getattr(usage, "candidates_token_count", 0),
                    "total_tokens": getattr(usage, "total_token_count", 0),
                }

            # Add thought summaries to reasoning_info for debugging/introspection
            if thought_summaries:
                if reasoning_info is None:
                    reasoning_info = {}
                reasoning_info["thought_summaries"] = thought_summaries

            return LLMOutput(
                content=content,
                tool_calls=tool_calls,
                reasoning_info=reasoning_info,
                provider_metadata=None,  # Thought signatures are now on individual tool calls
            )

        except Exception as e:
            # Map Google exceptions to our exception hierarchy
            error_message = str(e)

            if "401" in error_message or "api key" in error_message.lower():
                raise AuthenticationError(
                    error_message, provider="google", model=self.model_name
                ) from e
            elif "429" in error_message or "quota" in error_message.lower():
                raise RateLimitError(
                    error_message, provider="google", model=self.model_name
                ) from e
            elif "404" in error_message or "not found" in error_message.lower():
                raise ModelNotFoundError(
                    error_message, provider="google", model=self.model_name
                ) from e
            elif "token" in error_message.lower() and "limit" in error_message.lower():
                raise ContextLengthError(
                    error_message, provider="google", model=self.model_name
                ) from e
            elif "invalid" in error_message.lower() or "400" in error_message:
                raise InvalidRequestError(
                    error_message, provider="google", model=self.model_name
                ) from e
            elif (
                "connection" in error_message.lower()
                or "network" in error_message.lower()
            ):
                raise ProviderConnectionError(
                    error_message, provider="google", model=self.model_name
                ) from e
            elif "timeout" in error_message.lower():
                raise ProviderTimeoutError(
                    error_message, provider="google", model=self.model_name
                ) from e
            else:
                logger.error(f"Google GenAI API error: {e}", exc_info=True)
                raise LLMProviderError(
                    error_message, provider="google", model=self.model_name
                ) from e

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    ) -> dict[str, object]:
        """
        Format user message with optional file content.

        For the new API, file handling is different and would need to be adapted.
        """
        if not file_path:
            # No file, just text
            return {"role": "user", "content": prompt_text or ""}

        # For now, handle files as text content
        # The new API has different file upload mechanisms

        if mime_type and mime_type.startswith("image/"):
            logger.warning(
                "Image upload not implemented for new google-genai API - treating as text"
            )

        # Handle as text file
        async with aiofiles.open(file_path, encoding="utf-8") as f:
            file_content = await f.read()

        # Apply max length if specified
        if max_text_length and len(file_content) > max_text_length:
            file_content = file_content[:max_text_length]
            logger.info(f"Truncated file content to {max_text_length} characters")

        # Combine prompt and file content
        if prompt_text:
            combined_content = f"{prompt_text}\n\nFile content:\n{file_content}"
        else:
            combined_content = file_content

        return {"role": "user", "content": combined_content}

    def generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Generate streaming response using Google GenAI."""
        return self._generate_response_stream(messages, tools, tool_choice)

    async def _generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Internal async generator for streaming responses using Google GenAI."""
        try:
            # Keep messages as typed objects for processing
            typed_messages = list(messages)

            # Debug logging if enabled
            if self.should_debug_messages:
                logger.info(
                    f"=== LLM Streaming Request to {self.model_name} ===\n"
                    f"{_format_messages_for_debug(typed_messages, tools, tool_choice)}"
                )

            # Process tool attachments with typed messages
            processed_typed_messages = self._process_tool_messages(typed_messages)

            # Convert messages to format expected by API
            # _convert_messages_to_genai_format expects typed LLMMessage objects
            contents = self._convert_messages_to_genai_format(processed_typed_messages)

            # Debug: Log post-processed messages if enabled
            if self.should_debug_messages:
                logger.info(
                    f"=== After _process_tool_messages ({len(processed_typed_messages)} messages) ===\n"
                    f"{_format_messages_for_debug(processed_typed_messages, None, None)}"
                )

            # Build generation config
            config_params = {
                **self.default_kwargs,
                **self._get_model_specific_params(self.model_name),
            }

            # Map common parameters
            generation_config = types.GenerateContentConfig()
            # Set media resolution to HIGH for all requests (affects images, PDFs, videos)
            generation_config.media_resolution = (
                types.MediaResolution.MEDIA_RESOLUTION_HIGH
            )

            if "temperature" in config_params:
                generation_config.temperature = config_params["temperature"]
            if "max_tokens" in config_params:
                generation_config.max_output_tokens = config_params["max_tokens"]
            if "top_p" in config_params:
                generation_config.top_p = config_params["top_p"]
            if "top_k" in config_params:
                generation_config.top_k = config_params["top_k"]

            # Prepare all tools (function tools + grounding tools)
            all_tools = self._prepare_all_tools(tools)

            # Add tools to config if any are available
            if all_tools:
                generation_config.tools = all_tools
                # Disable automatic function calling so we can manually handle
                # function calls and thought signatures
                generation_config.automatic_function_calling = (
                    types.AutomaticFunctionCallingConfig(disable=True)
                )
                # TODO: The tool_choice parameter is not currently mapped to Google's API
                # This matches the behavior of the non-streaming implementation

            # Make streaming API call using generate_content_stream
            stream_response = await self.client.aio.models.generate_content_stream(
                model=self.model_name,
                contents=contents,
                config=generation_config,
            )

            # Track tool calls and thought summaries being accumulated
            accumulated_tool_calls = []
            thought_summaries = []
            part_index = 0

            # Process stream chunks
            async for chunk in stream_response:  # type: ignore[misc]
                # Extract text content from chunk
                if hasattr(chunk, "text") and chunk.text:
                    yield LLMStreamEvent(type="content", content=chunk.text)

                # Handle candidates structure for more complex responses
                elif hasattr(chunk, "candidates") and chunk.candidates:
                    for candidate in chunk.candidates:
                        if (
                            hasattr(candidate, "content")
                            and candidate.content
                            and hasattr(candidate.content, "parts")
                            and candidate.content.parts is not None  # Fix None check
                        ):
                            for part in candidate.content.parts:
                                # Extract thought summary if present (readable for debugging/introspection)
                                is_thought = hasattr(part, "thought") and part.thought
                                if is_thought:
                                    # When part.thought is True, the thought text is in part.text
                                    thought_text = getattr(part, "text", "")
                                    thought_summaries.append({
                                        "part_index": part_index,
                                        "summary": thought_text,
                                    })

                                # Handle text parts - but skip thought parts (they're for debugging only)
                                if (
                                    not is_thought
                                    and hasattr(part, "text")
                                    and part.text
                                ):
                                    yield LLMStreamEvent(
                                        type="content", content=part.text
                                    )

                                # Accumulate function calls with their thought signatures
                                if (
                                    hasattr(part, "function_call")
                                    and part.function_call
                                ):
                                    func_call = part.function_call
                                    if func_call.name:
                                        # Generate a unique ID for the tool call
                                        tool_call_id = f"call_{uuid.uuid4().hex[:24]}"

                                        # Check if this part also has a thought signature
                                        thought_sig = None
                                        if (
                                            hasattr(part, "thought_signature")
                                            and part.thought_signature
                                        ):
                                            # DEBUG LOGGING
                                            sig_len = len(part.thought_signature)
                                            sig_preview = part.thought_signature[:10]
                                            logger.warning(
                                                f"DEBUG: Received thought_signature from SDK. Len: {sig_len}, Preview: {sig_preview!r}"
                                            )

                                            # Wrap in opaque GeminiThoughtSignature - no processing
                                            thought_sig = GeminiThoughtSignature(
                                                part.thought_signature
                                            )
                                            if self.should_debug_messages:
                                                logger.info(
                                                    "Captured thought_signature for tool_call %s (len=%d)",
                                                    tool_call_id,
                                                    len(part.thought_signature),
                                                )
                                        elif self.should_debug_messages:
                                            logger.info(
                                                "No thought_signature present for tool_call %s",
                                                tool_call_id,
                                            )

                                        # Store func_call with its signature for later emission
                                        accumulated_tool_calls.append((
                                            tool_call_id,
                                            func_call,
                                            thought_sig,
                                        ))

                                part_index += 1

            # Emit accumulated tool calls, each with its own thought signature if present
            for tool_call_id, func_call, thought_sig in accumulated_tool_calls:
                # Build provider_metadata for this specific tool call if it has a signature
                provider_metadata = None
                if thought_sig:
                    # Create GeminiProviderMetadata object with thought signature
                    provider_metadata = GeminiProviderMetadata(
                        thought_signature=thought_sig
                    )

                # Preserve the original args structure from the SDK to avoid any re-encoding
                args_value = func_call.args if func_call.args is not None else {}
                func_call_id = func_call.id if isinstance(func_call.id, str) else None
                call_id = func_call_id or tool_call_id

                tool_call = ToolCallItem(
                    id=call_id,
                    type="function",
                    function=ToolCallFunction(
                        name=func_call.name,
                        arguments=args_value,
                    ),
                    provider_metadata=provider_metadata,
                )
                yield LLMStreamEvent(
                    type="tool_call", tool_call=tool_call, tool_call_id=tool_call_id
                )

            # Signal completion
            # Note: Usage metadata might not be available in streaming mode
            done_metadata = {}

            # Add thought summaries to reasoning_info for debugging/introspection
            if thought_summaries:
                if "reasoning_info" not in done_metadata:
                    done_metadata["reasoning_info"] = {}
                done_metadata["reasoning_info"]["thought_summaries"] = thought_summaries

            yield LLMStreamEvent(type="done", metadata=done_metadata)

        except Exception as e:
            # Handle errors the same way as non-streaming
            error_message = str(e)

            # Categorize the error type for metadata
            error_type = "unknown"
            if "401" in error_message or "api key" in error_message.lower():
                error_type = "authentication"
            elif "429" in error_message or "quota" in error_message.lower():
                error_type = "rate_limit"
            elif "404" in error_message or "not found" in error_message.lower():
                error_type = "model_not_found"
            elif "token" in error_message.lower() and "limit" in error_message.lower():
                error_type = "context_length"
            elif "invalid" in error_message.lower() or "400" in error_message:
                error_type = "invalid_request"
            elif (
                "connection" in error_message.lower()
                or "network" in error_message.lower()
            ):
                error_type = "connection"
            elif "timeout" in error_message.lower():
                error_type = "timeout"

            logger.error(
                f"Google GenAI streaming error ({error_type}): {e}", exc_info=True
            )
            yield LLMStreamEvent(
                type="error",
                error=error_message,
                metadata={
                    "error_id": str(e.__class__.__name__),
                    "error_type": error_type,
                    "provider": "google",
                    "model": self.model_name,
                },
            )
