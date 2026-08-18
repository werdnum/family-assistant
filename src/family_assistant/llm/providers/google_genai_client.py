"""
Direct Google Generative AI (Gemini) implementation for LLM interactions.
"""

import base64
import json
import logging
import mimetypes
import os
import re
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolAttachment

import aiofiles
from google import genai
from google.genai import types
from google.genai.client import DebugConfig
from google.genai.interactions import Interaction
from opentelemetry import trace
from pydantic import BaseModel, ValidationError

from family_assistant.config_models import AntigravityEnvironmentConfig
from family_assistant.llm import (
    BaseLLMClient,
    JsonObject,
    LLMOutput,
    LLMStreamEvent,
    StreamEventMetadata,
    StructuredOutputError,
    ToolCallFunction,
    ToolCallItem,
    UserMessageDict,
    _format_messages_for_debug,
    describe_attachment_for_fallback,
)
from family_assistant.llm.antigravity_egress import (
    AntigravityEgressResolver,
    EgressNetworkResolver,
)
from family_assistant.llm.google_types import (
    GeminiProviderMetadata,
    GeminiThoughtSignature,
)
from family_assistant.llm.messages import (
    AssistantMessage,
    ImageUrlContentPart,
    LLMMessage,
    MessageReasoningInfo,
    TextContentPart,
    ToolMessage,
    UserMessage,
    is_turn_scaffolding,
)
from family_assistant.llm.utils.call_telemetry import LLMCallTelemetry
from family_assistant.processing.protocol import (
    DelegationPermanentError,
    DelegationTaskNotFoundError,
    DelegationTransientError,
)
from family_assistant.tools.computer_use_names import COMPUTER_USE_FUNCTION_NAMES
from family_assistant.tools.types import ToolDefinition, normalize_json_schema_type

from ..base import (
    AuthenticationError,
    ContextLengthError,
    InvalidRequestError,
    LLMProviderError,
    ModelNotFoundError,
    ProviderConnectionError,
    ProviderTimeoutError,
    RateLimitError,
    ServiceUnavailableError,
)

logger = logging.getLogger(__name__)


def _system_prefixed_content(content: str) -> str:
    """Prefix system content for Gemini without duplicating an existing marker."""
    if content.startswith("System:"):
        return content
    return f"System: {content}"


tracer = trace.get_tracer(__name__)


def _first_finish_reason(
    response: types.GenerateContentResponse,
) -> types.FinishReason | None:
    """The finish reason of the first candidate, when the response carries one."""
    for candidate in response.candidates or []:
        if candidate.finish_reason is not None:
            return candidate.finish_reason
    return None


T = TypeVar("T", bound=BaseModel)

# Maps Interactions API ErrorEvent.error.code values to our typed exception classes.
# See https://cloud.google.com/apis/design/errors for canonical error codes.
_STREAM_ERROR_CODE_TO_EXCEPTION: dict[str, type[LLMProviderError]] = {
    "resource_exhausted": RateLimitError,
    "deadline_exceeded": ProviderTimeoutError,
    "unauthenticated": AuthenticationError,
    "permission_denied": AuthenticationError,
    "not_found": ModelNotFoundError,
    "invalid_argument": InvalidRequestError,
    "internal": ServiceUnavailableError,
    "unavailable": ServiceUnavailableError,
}

# Maps SDK HTTP status codes (from APIStatusError.status_code) to our typed exceptions.
_SDK_STATUS_CODE_TO_EXCEPTION: dict[int, type[LLMProviderError]] = {
    400: InvalidRequestError,
    401: AuthenticationError,
    403: AuthenticationError,
    404: ModelNotFoundError,
    429: RateLimitError,
}

_INTERACTION_TERMINAL_ERROR_STATUSES = {
    "failed",
    "cancelled",
    "incomplete",
    "budget_exceeded",
}


def is_interaction_terminal_error_status(status: str) -> bool:
    """Check if an Interaction status is a terminal (non-success) end state.

    Deliberately a deny-list, not an allow-list of "pending" statuses: the
    Interactions API can report statuses this SDK doesn't enumerate (e.g. a
    capacity-queueing "queued" state alongside the known ``in_progress``/
    ``requires_action``), and any such status must be treated as still
    pending rather than failed. Mirrors the streaming path's own
    ``interaction.status_update`` handling.
    """
    return status in _INTERACTION_TERMINAL_ERROR_STATUSES


def is_deep_research_model(model: str) -> bool:
    """Check if a model identifier corresponds to a Deep Research agent."""
    return "deep-research" in model


def is_antigravity_model(model: str) -> bool:
    """Check if a model identifier corresponds to an Antigravity managed agent.

    A prefix match rather than an equality test against the current preview
    id, so a later ``antigravity-*`` revision routes through the same path
    without a code change -- the same stance ``is_deep_research_model`` takes
    on the Deep Research tiers. The ``models/`` prefix the client attaches to
    every model id is stripped first, so this holds for both the configured
    id and the client's own ``model_name``.
    """
    return model.removeprefix("models/").startswith("antigravity")


def is_interactions_agent_model(model: str) -> bool:
    """Whether a model identifier names an Interactions API agent.

    These do not go through ``generateContent`` at all: they are submitted to
    ``interactions.create`` with an ``agent=`` rather than a ``model=``, and
    run server-side until they reach a terminal state.
    """
    return is_deep_research_model(model) or is_antigravity_model(model)


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
    except Exception:
        pass

    return raw_value


def convert_tools_to_genai_format(tools: list[ToolDefinition]) -> list[Any]:
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
            prop_type, prop_nullable = normalize_json_schema_type(
                prop_def.get("type", "string")
            )

            if prop_type == "array":
                # Handle array types - need to specify items
                items_def = prop_def.get("items", {})
                items_type_name, items_nullable = normalize_json_schema_type(
                    items_def.get("type", "string")
                )
                items_type = types.Type(items_type_name.upper())

                google_properties[prop_name] = types.Schema(
                    type=types.Type.ARRAY,
                    description=prop_def.get("description", ""),
                    nullable=prop_nullable or None,
                    items=types.Schema(
                        type=items_type,
                        description=items_def.get("description", ""),
                        nullable=items_nullable or None,
                    ),
                )
            else:
                # Handle non-array types
                schema_type = types.Type(prop_type.upper())
                google_properties[prop_name] = types.Schema(
                    type=schema_type,
                    description=prop_def.get("description", ""),
                    nullable=prop_nullable or None,
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


class GoogleGenAIClient(BaseLLMClient):
    """Direct Google Generative AI implementation."""

    def __init__(
        self,
        api_key: str,
        model: str,
        model_parameters: dict[str, dict[str, object]] | None = None,
        api_base: str | None = None,
        enable_url_context: bool = False,
        enable_google_search: bool = False,
        enable_computer_use: bool = False,
        computer_use_excluded_functions: list[str] | None = None,
        antigravity_model: str | None = None,
        antigravity_max_total_tokens: int | None = None,
        antigravity_environment: AntigravityEnvironmentConfig | None = None,
        antigravity_egress_resolver: EgressNetworkResolver | None = None,
        debug_messages: bool | None = None,
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
            enable_computer_use: Enable native Gemini computer use tools (explicit opt-in)
            computer_use_excluded_functions: List of computer use function names to exclude
            antigravity_model: Reasoning model for an Antigravity managed-agent model id
            antigravity_max_total_tokens: Token ceiling for one Antigravity agent run
            antigravity_environment: Sandbox environment (egress policy and injected
                credentials) for an Antigravity agent run
            antigravity_egress_resolver: Pre-built resolver for that environment,
                primarily for tests; built from ``antigravity_environment`` when omitted
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
        self.enable_computer_use = enable_computer_use
        self.computer_use_excluded_functions = computer_use_excluded_functions
        self.antigravity_model = antigravity_model
        self.antigravity_max_total_tokens = antigravity_max_total_tokens
        self.antigravity_environment = antigravity_environment
        # Only a resolver this client built is closed by it; an injected one
        # belongs to whoever passed it in.
        self._owned_antigravity_egress: AntigravityEgressResolver | None = None
        if antigravity_egress_resolver is not None:
            self._antigravity_egress: EgressNetworkResolver | None = (
                antigravity_egress_resolver
            )
        elif antigravity_environment is not None:
            self._owned_antigravity_egress = AntigravityEgressResolver(
                antigravity_environment
            )
            self._antigravity_egress = self._owned_antigravity_egress
        else:
            self._antigravity_egress = None

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
            f"computer use: {enable_computer_use}, "
            f"computer use excluded: {computer_use_excluded_functions}, "
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
                api_client.close()
        except Exception as e:
            logger.debug(f"Error closing API client: {e}")
        if self._owned_antigravity_egress is not None:
            await self._owned_antigravity_egress.aclose()

    async def __aenter__(self) -> "GoogleGenAIClient":
        """Enter async context manager."""
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:  # noqa: ANN401
        """Exit async context manager and close client."""
        await self.close()

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

    def _build_base_generation_config(self) -> types.GenerateContentConfig:
        """Build common GenerateContentConfig fields shared by native output modes."""
        config_params = {
            **self.default_kwargs,
            **self._get_model_specific_params(self.model_name),
        }

        generation_config = types.GenerateContentConfig()
        generation_config.media_resolution = types.MediaResolution.MEDIA_RESOLUTION_HIGH

        if "temperature" in config_params:
            generation_config.temperature = float(config_params["temperature"])
        if "max_tokens" in config_params:
            generation_config.max_output_tokens = int(config_params["max_tokens"])
        if "top_p" in config_params:
            generation_config.top_p = float(config_params["top_p"])
        if "top_k" in config_params:
            generation_config.top_k = int(config_params["top_k"])

        return generation_config

    def _extract_text_response(self, response: object) -> str | None:
        """Extract text content from a GenerateContent response."""
        response_obj = cast("Any", response)

        if hasattr(response_obj, "text") and response_obj.text:
            return cast("str", response_obj.text)

        if hasattr(response_obj, "candidates") and response_obj.candidates:
            candidates = response_obj.candidates
            candidate = candidates[0]
            if (
                hasattr(candidate, "content")
                and candidate.content
                and hasattr(candidate.content, "parts")
            ):
                text_parts: list[str] = []
                for part in candidate.content.parts:
                    is_thought = hasattr(part, "thought") and part.thought
                    if not is_thought and hasattr(part, "text") and part.text:
                        text_parts.append(part.text)
                if text_parts:
                    return "".join(text_parts)

        return None

    async def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
        max_retries: int = 2,
    ) -> T:
        """Generate structured output using Gemini's native response_schema mode."""
        self._validate_user_input(messages)

        attempt_messages = list(messages)
        generation_config = self._build_base_generation_config()
        generation_config.response_mime_type = "application/json"
        generation_config.response_schema = response_model

        raw_response: str | None = None
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                contents = self._convert_messages_to_genai_format(
                    self._process_tool_messages(attempt_messages)
                )
                response = await self.client.aio.models.generate_content(
                    model=self.model_name,
                    contents=contents,
                    config=generation_config,
                )
                raw_response = self._extract_text_response(response)
                if not raw_response:
                    raise ValueError("LLM returned empty content")
                return response_model.model_validate_json(raw_response)
            except (ValidationError, json.JSONDecodeError, ValueError) as e:
                last_error = e
                logger.warning(
                    "Gemini structured output validation failed "
                    f"(attempt {attempt + 1}/{max_retries + 1}): {e}"
                )
                if attempt < max_retries:
                    if raw_response is not None:
                        attempt_messages.append(AssistantMessage(content=raw_response))
                    attempt_messages.append(
                        UserMessage(
                            content=(
                                "Your previous response did not satisfy the required schema. "
                                f"Error: {e}\n\n"
                                "Please try again with valid structured JSON."
                            )
                        )
                    )
            except Exception as e:
                raise self._map_error_to_typed_exception(e) from e

        raise StructuredOutputError(
            message=f"Failed to generate valid structured output after {max_retries + 1} attempts",
            provider="google",
            model=self.model_name,
            raw_response=raw_response,
            validation_error=last_error,
        )

    async def generate_json(
        self,
        messages: Sequence[LLMMessage],
        max_retries: int = 2,
    ) -> JsonObject:
        """Generate a JSON object using Gemini's native JSON-object mode."""
        self._validate_user_input(messages)

        attempt_messages = self._add_system_instruction(
            messages,
            (
                "You must respond with a valid JSON object. "
                "The required fields and structure are defined by the conversation. "
                "Do not omit requested keys. Respond ONLY with the JSON object."
            ),
        )
        generation_config = self._build_base_generation_config()
        generation_config.response_mime_type = "application/json"

        raw_response: str | None = None
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                contents = self._convert_messages_to_genai_format(
                    self._process_tool_messages(attempt_messages)
                )
                response = await self.client.aio.models.generate_content(
                    model=self.model_name,
                    contents=contents,
                    config=generation_config,
                )
                raw_response = self._extract_text_response(response)
                if not raw_response:
                    raise ValueError("LLM returned empty content")
                return self._parse_json_object_response(raw_response)
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                last_error = e
                logger.warning(
                    "Gemini JSON output validation failed "
                    f"(attempt {attempt + 1}/{max_retries + 1}): {e}"
                )
                if attempt < max_retries:
                    if raw_response is not None:
                        attempt_messages.append(AssistantMessage(content=raw_response))
                    attempt_messages.append(
                        UserMessage(
                            content=(
                                f"Your previous response was not a valid JSON object. Error: {e}\n\n"
                                "Please try again and respond with a JSON object only."
                            )
                        )
                    )
            except Exception as e:
                raise self._map_error_to_typed_exception(e) from e

        raise StructuredOutputError(
            message=f"Failed to generate valid JSON output after {max_retries + 1} attempts",
            provider="google",
            model=self.model_name,
            raw_response=raw_response,
            validation_error=last_error,
        )

    def _supports_multimodal_tools(self) -> bool:
        """Check if model supports multimodal tool responses.

        This includes Gemini 3+ and Computer Use models which need
        screenshots in function responses.
        """
        model_lower = self.model_name.lower()
        return "gemini-3" in model_lower or self.enable_computer_use

    def _process_tool_messages(
        self,
        messages: list[LLMMessage],
    ) -> list[LLMMessage]:
        """Process tool messages, preserving attachments for multimodal-capable models.

        When multimodal tools are supported (Gemini 3+, Computer Use), we keep
        transient_attachments on ToolMessages so they can be included in the
        FunctionResponse as inline data blobs. Otherwise, we use the base class
        behavior which extracts attachments for injection as user messages.
        """
        if not self._supports_multimodal_tools():
            return super()._process_tool_messages(messages)

        # For multimodal-capable models, just return messages as-is
        # The attachments will be included in the FunctionResponse in
        # _convert_messages_to_genai_format
        return list(messages)

    def create_attachment_injection(
        self,
        attachment: "ToolAttachment",
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
        parts: list[dict[str, object] | types.Part] = [
            {"text": "[System: File from previous tool response]"}
        ]

        # Gemini supports images, videos, audio, and PDFs via types.Part.from_bytes()
        if attachment.content and (
            attachment.mime_type.startswith("image/")
            or attachment.mime_type.startswith("video/")
            or attachment.mime_type.startswith("audio/")
            or attachment.mime_type == "application/pdf"
        ):
            # Use the recommended types.Part.from_bytes() method for multimodal content
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
                        # Gemini supports images, videos, audio, and PDFs
                        if effective_mime_type and (
                            effective_mime_type.startswith("image/")
                            or effective_mime_type.startswith("video/")
                            or effective_mime_type.startswith("audio/")
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
                    "text": f"[File: {attachment.file_path} - Error reading file: {e!s}]"
                })

        # `parts` carries the media for Gemini, which reads `parts` and ignores
        # `content`. Every other adapter does the reverse, and a cross-provider
        # fallback renders this same message -- `RetryingLLMClient` builds the
        # injection from the primary alone -- so `content` is what the fallback
        # sees, and a placeholder there would drop the attachment silently.
        return UserMessage(
            content=describe_attachment_for_fallback(attachment),
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
                system_content = content if isinstance(content, str) else ""
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part(text=_system_prefixed_content(system_content))
                        ],
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
                            except Exception as e:
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
                            # Workaround for Gemini API: if no thought signature is present for a
                            # function call, we must provide a dummy signature to avoid validation
                            # errors.
                            # See: https://ai.google.dev/gemini-api/docs/thought-signatures (FAQ section)
                            assistant_parts.append(
                                types.Part(
                                    function_call=types.FunctionCall(
                                        name=func_name,
                                        args=args_dict,
                                        id=func_id,
                                    ),
                                    thought_signature=b"skip_thought_signature_validator",
                                )
                            )

                if assistant_parts:
                    contents.append(types.Content(role="model", parts=assistant_parts))
            elif role == "tool":
                # Handle tool responses
                if not isinstance(msg, ToolMessage):
                    continue

                tool_content = msg.content

                # For Computer Use, we need to extract the URL from the response
                # The content may have attachment IDs appended (e.g., "[Attachment ID(s): ...]")
                # which breaks JSON parsing, so we strip that suffix first
                json_content = tool_content
                if isinstance(json_content, str):
                    # Strip attachment ID suffix if present
                    attachment_marker = "\n[Attachment ID(s):"
                    if attachment_marker in json_content:
                        json_content = json_content.split(attachment_marker)[0]

                # Try to parse the content as JSON
                try:
                    response_data = (
                        json.loads(json_content)
                        if isinstance(json_content, str)
                        else json_content
                    )
                except json.JSONDecodeError:
                    response_data = {"result": tool_content}

                # SDK requires response to be a dict, not a primitive value
                if not isinstance(response_data, dict):
                    response_data = {"result": response_data}

                # Prepare FunctionResponse arguments
                function_response_args = {
                    "id": msg.tool_call_id,
                    "name": msg.name,
                    "response": response_data,
                }

                # Debug logging for Computer Use
                logger.debug(
                    f"Building FunctionResponse for '{msg.name}': "
                    f"response_data={response_data}, "
                    f"has_attachments={msg.transient_attachments is not None}"
                )

                # Handle multimodal attachments for Gemini 3+
                if self._supports_multimodal_tools() and msg.transient_attachments:
                    fr_parts = []
                    for att in msg.transient_attachments:
                        if att.content:
                            fr_parts.append(
                                types.FunctionResponsePart(
                                    inline_data=types.FunctionResponseBlob(
                                        mime_type=att.mime_type,
                                        data=att.content,
                                        display_name=att.description
                                        or att.attachment_id,
                                    )
                                )
                            )
                        # We currently only support inline data (bytes) for simplicity
                        # file_path would require uploading to File API or reading to bytes
                        # but ToolAttachment usually populates content for small files.

                    if fr_parts:
                        function_response_args["parts"] = fr_parts

                # Create FunctionResponse using SDK types with snake_case
                part = types.Part(
                    function_response=types.FunctionResponse(**function_response_args),
                )
                # Function responses are user turns in the Gemini conversation
                # schema. Gemini 3.6 rejects the legacy "function" role even for
                # text-only responses.
                contents.append(types.Content(role="user", parts=[part]))

        return contents

    def _convert_tools_to_genai_format(self, tools: list[ToolDefinition]) -> list[Any]:
        """Convert OpenAI-style tools to Gemini format."""
        # If Computer Use is enabled, filter out manually-defined computer use tools
        # because we use types.Tool(computer_use=...) instead
        if self.enable_computer_use:
            filtered_tools = [
                tool
                for tool in tools
                if tool.get("function", {}).get("name")
                not in COMPUTER_USE_FUNCTION_NAMES
            ]
            return convert_tools_to_genai_format(filtered_tools)

        return convert_tools_to_genai_format(tools)

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
        self,
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> list[Any]:
        """Prepare all tools including function tools and grounding tools.

        Args:
            tools: List of tool definitions in OpenAI format
            tool_choice: Tool choice mode - "auto", "none", "required", or specific tool name.
                        When "none", returns empty list to prevent any tool calls.
        """
        # When tool_choice is "none", don't include any tools at all
        # This forces the model to return a text response
        if tool_choice == "none":
            return []

        all_tools: list[Any] = []

        # Add Computer Use tool if enabled
        if self.enable_computer_use:
            computer_use_tool = types.Tool(
                computer_use=types.ComputerUse(
                    environment=types.Environment.ENVIRONMENT_BROWSER,
                    enable_prompt_injection_detection=True,
                    excluded_predefined_functions=(
                        list(self.computer_use_excluded_functions)
                        if self.computer_use_excluded_functions
                        else None
                    ),
                )
            )
            all_tools.append(computer_use_tool)
            logger.debug("Added Computer Use tool for browser environment")

        # Add function tools if provided
        if tools:
            function_tools = self._convert_tools_to_genai_format(tools)
            all_tools.extend(function_tools)

        # Add grounding tools (URL context and Google Search)
        grounding_tools = self._create_grounding_tools()
        all_tools.extend(grounding_tools)

        return all_tools

    @property
    def _agent_label(self) -> str:
        """Human-readable name of the Interactions agent, for logs and errors."""
        return (
            "Antigravity" if is_antigravity_model(self._agent_name) else "Deep Research"
        )

    @property
    def _agent_name(self) -> str:
        """The bare agent id to send as ``agent=`` on an Interactions call."""
        return self.model_name.replace("models/", "")

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Generate response using Google GenAI."""
        # Validate user input before processing
        self._validate_user_input(messages)

        with tracer.start_as_current_span("llm.provider.generate") as span:
            telemetry = LLMCallTelemetry(
                span,
                provider="google",
                system="google-genai",
                requested_model=self.model_name,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                streaming=False,
            )

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
                contents = self._convert_messages_to_genai_format(
                    processed_typed_messages
                )

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
                            ts_len = (
                                len(ts) if isinstance(ts, (bytes, bytearray)) else 0
                            )
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
                    generation_config.temperature = float(config_params["temperature"])
                if "max_tokens" in config_params:
                    generation_config.max_output_tokens = int(
                        config_params["max_tokens"]
                    )
                if "top_p" in config_params:
                    generation_config.top_p = float(config_params["top_p"])
                if "top_k" in config_params:
                    generation_config.top_k = int(config_params["top_k"])

                # Prepare all tools (function tools + grounding tools)
                # Pass tool_choice to respect "none" mode which returns empty list
                all_tools = self._prepare_all_tools(tools, tool_choice)

                # Add tools to config if any are available
                if all_tools:
                    generation_config.tools = all_tools
                    # Disable automatic function calling so we can manually handle
                    # function calls and thought signatures
                    generation_config.automatic_function_calling = (
                        types.AutomaticFunctionCallingConfig(disable=True)
                    )

                # Configure tool behavior based on tool_choice setting
                if tool_choice == "none":
                    # Explicitly configure the model to not call functions
                    # This ensures the model returns text even if tools were provided earlier in conversation
                    generation_config.tool_config = types.ToolConfig(
                        function_calling_config=types.FunctionCallingConfig(
                            mode=types.FunctionCallingConfigMode.NONE
                        )
                    )
                elif tool_choice == "required":
                    # Force the model to call any available tool
                    generation_config.tool_config = types.ToolConfig(
                        function_calling_config=types.FunctionCallingConfig(
                            mode=types.FunctionCallingConfigMode.ANY
                        )
                    )
                elif tool_choice and tool_choice not in {"auto", "required"}:
                    # Specific tool name: restrict to calling only this tool
                    generation_config.tool_config = types.ToolConfig(
                        function_calling_config=types.FunctionCallingConfig(
                            mode=types.FunctionCallingConfigMode.ANY,
                            allowed_function_names=[tool_choice],
                        )
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
                                if (
                                    not is_thought
                                    and hasattr(part, "text")
                                    and part.text
                                ):
                                    text_parts.append(part.text)
                            if text_parts:
                                content = "".join(text_parts)

                # Extract tool calls and thought signatures from response
                tool_calls = None
                thought_summaries: list[dict[str, str | int]] = []

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
                                        "Received a tool call without a name: %s",
                                        func_call,
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
                                    func_call.id
                                    if isinstance(func_call.id, str)
                                    else None
                                )
                                call_id = (
                                    func_call_id or f"call_{uuid.uuid4().hex[:24]}"
                                )

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
                reasoning_info: MessageReasoningInfo | None = None
                if hasattr(response, "usage_metadata") and response.usage_metadata:
                    usage = response.usage_metadata
                    reasoning_info = self._reasoning_info_from_usage_metadata(usage)

                # Add thought summaries to reasoning_info for debugging/introspection
                if thought_summaries:
                    if reasoning_info is None:
                        reasoning_info = MessageReasoningInfo()
                    reasoning_info["thought_summaries"] = thought_summaries

                telemetry.record_response_metadata(
                    resolved_model=response.model_version,
                    response_id=response.response_id,
                    finish_reason=_first_finish_reason(response),
                )

                llm_output = LLMOutput(
                    content=content,
                    tool_calls=tool_calls,
                    reasoning_info=telemetry.finalize_usage(reasoning_info),
                    provider_metadata=None,  # Thought signatures are now on individual tool calls
                    resolved_model=response.model_version,
                )

                telemetry.record_output(llm_output)
                telemetry.finish_success(asdict(llm_output))

                return llm_output

            except Exception as e:
                telemetry.finish_error(e)
                raise self._map_error_to_typed_exception(e) from e

    def _map_error_to_typed_exception(self, e: Exception) -> LLMProviderError:
        """Map a raw exception to a typed LLMProviderError subclass."""
        error_message = str(e)

        if "401" in error_message or "api key" in error_message.lower():
            return AuthenticationError(
                error_message, provider="google", model=self.model_name
            )
        elif "429" in error_message or "quota" in error_message.lower():
            return RateLimitError(
                error_message,
                provider="google",
                model=self.model_name,
                retry_after=self._parse_retry_after(error_message),
            )
        elif "404" in error_message or "not found" in error_message.lower():
            return ModelNotFoundError(
                error_message, provider="google", model=self.model_name
            )
        elif "token" in error_message.lower() and "limit" in error_message.lower():
            return ContextLengthError(
                error_message, provider="google", model=self.model_name
            )
        elif "invalid" in error_message.lower() or "400" in error_message:
            return InvalidRequestError(
                error_message, provider="google", model=self.model_name
            )
        elif (
            "connection" in error_message.lower() or "network" in error_message.lower()
        ):
            return ProviderConnectionError(
                error_message, provider="google", model=self.model_name
            )
        elif "timeout" in error_message.lower():
            return ProviderTimeoutError(
                error_message, provider="google", model=self.model_name
            )
        else:
            logger.error(f"Google GenAI API error: {e}", exc_info=e)
            return LLMProviderError(
                error_message, provider="google", model=self.model_name
            )

    @staticmethod
    def _reasoning_info_from_usage_metadata(
        usage: Any,  # noqa: ANN401 - types.GenerateContentResponseUsageMetadata
    ) -> MessageReasoningInfo:
        """Build reasoning info from Gemini's `usage_metadata`.

        Gemini caches implicitly and reports hits in `cached_content_token_count`
        as a *subset* of `prompt_token_count`, so cached tokens are not added to
        the total the way Anthropic's separate buckets are.
        """
        reasoning_info = MessageReasoningInfo(
            prompt_tokens=getattr(usage, "prompt_token_count", 0) or 0,
            completion_tokens=getattr(usage, "candidates_token_count", 0) or 0,
            total_tokens=getattr(usage, "total_token_count", 0) or 0,
        )
        # A reported zero is a known cache miss; only an absent field is left
        # out, so a miss stays distinguishable from "provider reported nothing".
        cached_tokens = getattr(usage, "cached_content_token_count", None)
        if cached_tokens is not None:
            reasoning_info["cached_prompt_tokens"] = cached_tokens
        thoughts_tokens = getattr(usage, "thoughts_token_count", None)
        if thoughts_tokens is not None:
            reasoning_info["reasoning_tokens"] = thoughts_tokens
        return reasoning_info

    @staticmethod
    def _parse_retry_after(error_message: str) -> float | None:
        """Extract retry delay from Gemini error messages.

        Gemini errors may include 'retryDelay: 36s' or similar patterns.
        """
        match = re.search(r"retryDelay[\":\s]*(\d+(?:\.\d+)?)s", error_message)
        if match:
            return float(match.group(1))
        return None

    def _map_interactions_error(self, e: Exception) -> LLMProviderError:
        """Map an Interactions API exception to a typed LLMProviderError.

        Uses duck-typing to check for status_code (SDK APIStatusError) and
        falls back to string-based matching for non-HTTP errors.
        """
        error_message = str(e)
        status_code: int | None = getattr(e, "status_code", None)

        if status_code is not None:
            exc_class = _SDK_STATUS_CODE_TO_EXCEPTION.get(status_code)
            if exc_class:
                if exc_class is RateLimitError:
                    return RateLimitError(
                        error_message,
                        provider="google",
                        model=self.model_name,
                        retry_after=self._parse_retry_after(error_message),
                    )
                return exc_class(
                    error_message, provider="google", model=self.model_name
                )
            if status_code >= 500:
                return ServiceUnavailableError(
                    error_message, provider="google", model=self.model_name
                )

        return self._map_error_to_typed_exception(e)

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
    ) -> UserMessageDict:
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
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Generate streaming response using Google GenAI."""
        # Validate user input before processing
        self._validate_user_input(messages)

        if is_interactions_agent_model(self.model_name):
            return self._generate_agent_interaction_stream(messages)
        return self._generate_response_stream(messages, tools, tool_choice)

    def _extract_agent_input_text(self, messages: Sequence[LLMMessage]) -> str:
        """Collapse the trailing run of user messages into one input string.

        Interactions agents take a single ``input`` string rather than a
        message list, so scaffolding is dropped rather than serialized: the
        turn-context block would otherwise be appended verbatim to the request
        the agent is asked to carry out.
        """
        # Collect all contiguous trailing user messages for input
        # This handles cases where user sends multiple messages in a row
        user_input_parts: list[str] = []
        for msg in reversed(messages):
            if is_turn_scaffolding(msg):
                continue
            if msg.role != "user":
                # Stop at the first non-user message
                break

            self._reject_unrepresentable_input(msg)

            msg_text = ""
            if isinstance(msg.content, str):
                msg_text = msg.content
            elif isinstance(msg.content, list):
                text_fragments = []
                for part in msg.content:
                    if isinstance(part, TextContentPart):
                        text_fragments.append(part.text)
                    elif isinstance(part, str):
                        text_fragments.append(part)
                msg_text = "\n".join(text_fragments)

            if msg_text:
                user_input_parts.insert(0, msg_text)

        return "\n\n".join(user_input_parts)

    def _reject_unrepresentable_input(self, msg: LLMMessage) -> None:
        """Refuse a message whose non-text payload the agent cannot receive.

        An Interactions agent takes one ``input`` string, so anything that is
        not text -- an image or PDF injected as provider ``parts``, an image
        URL part -- is unrepresentable. Dropping it silently would hand the
        agent a request that names a file it never received and let it answer
        confidently about nothing, so the turn fails instead. Text-shaped
        attachments (``text/*``, JSON, CSV) are unaffected: the base adapter
        injects those as text, which this path carries.
        """
        if getattr(msg, "parts", None):
            raise InvalidRequestError(
                f"{self._agent_label} cannot read attachments: this request "
                "carries media the agent has no way to receive. Send the "
                "content as text, or use a profile that reads attachments.",
                provider="google",
                model=self.model_name,
            )
        if isinstance(msg.content, list):
            unsupported = sorted({
                part.type
                for part in msg.content
                if not isinstance(part, str | TextContentPart)
                and getattr(part, "type", None) is not None
            })
            if unsupported:
                raise InvalidRequestError(
                    f"{self._agent_label} cannot read {', '.join(unsupported)} "
                    "content: the agent takes a single text request. Send the "
                    "content as text, or use a profile that reads attachments.",
                    provider="google",
                    model=self.model_name,
                )

    def _extract_agent_system_prompt(self, messages: Sequence[LLMMessage]) -> str:
        """Concatenate every system message into one instruction string."""
        system_prompt = ""
        for msg in messages:
            if msg.role == "system" and msg.content:
                system_prompt += f"{_system_prefixed_content(msg.content)}\n\n"
        return system_prompt

    def _resolve_previous_interaction_id(
        self, messages: Sequence[LLMMessage], explicit: str | None
    ) -> str | None:
        """Find the interaction to continue from, preferring an explicit id.

        An explicit value (e.g. a delegation run's stored interaction id) wins;
        otherwise it is scanned out of the last assistant message's provider
        metadata, which is where the interactive chat path records it.
        """
        if explicit is not None:
            return explicit

        for msg in reversed(messages):
            if (
                msg.role == "assistant"
                and isinstance(msg, AssistantMessage)
                and msg.provider_metadata
            ):
                pm = msg.provider_metadata
                if isinstance(pm, GeminiProviderMetadata) and pm.interaction_id:
                    return pm.interaction_id
                if isinstance(pm, dict) and pm.get("interaction_id"):
                    return cast("str | None", pm.get("interaction_id"))
        return None

    def _build_agent_create_kwargs(
        self,
        messages: Sequence[LLMMessage],
        *,
        previous_interaction_id: str | None = None,
        # ast-grep-ignore: no-dict-any - **kwargs for the Interactions SDK's create()
    ) -> dict[str, Any]:
        """Build ``interactions.create`` kwargs for an Interactions agent call.

        Shapes the input for whichever agent ``model_name`` names — Deep
        Research or Antigravity — and resolves ``previous_interaction_id`` for
        multi-turn continuation. Does not set ``stream`` — callers choose
        streaming (interactive) or not (submit-then-poll).
        """
        resolved_previous_interaction_id = self._resolve_previous_interaction_id(
            messages, previous_interaction_id
        )
        input_text = self._extract_agent_input_text(messages)
        system_prompt = self._extract_agent_system_prompt(messages)

        # ast-grep-ignore: no-dict-any - **kwargs for the Interactions SDK's create()
        create_kwargs: dict[str, Any] = {
            "agent": self._agent_name,
            "background": True,
        }

        if is_antigravity_model(self._agent_name):
            create_kwargs["agent_config"] = self._antigravity_agent_config()
            # Antigravity takes a first-class system_instruction, so the prompt
            # stays out of the task the agent plans against instead of being
            # read as part of it.
            if system_prompt.strip():
                create_kwargs["system_instruction"] = system_prompt.strip()
        else:
            create_kwargs["agent_config"] = {
                "type": "deep-research",
                "thinking_summaries": "auto",
                "visualization": "auto",
            }
            # Deep Research exposes no system_instruction, so the prompt is
            # folded into the single input string.
            if system_prompt:
                input_text = system_prompt + input_text

        create_kwargs["input"] = input_text

        if not input_text:
            raise InvalidRequestError(
                f"{self._agent_label} requires non-empty input",
                provider="google",
                model=self.model_name,
            )

        logger.info(
            f"Starting {self._agent_label} interaction. Model: {self.model_name}, "
            f"Prev ID: {resolved_previous_interaction_id}"
        )

        if resolved_previous_interaction_id:
            create_kwargs["previous_interaction_id"] = resolved_previous_interaction_id
        return create_kwargs

    # ast-grep-ignore: no-dict-any - agent_config payload for the Interactions SDK
    def _antigravity_agent_config(self) -> dict[str, Any]:
        """Build the ``agent_config`` block for an Antigravity agent run.

        ``model`` and ``max_total_tokens`` are omitted when unset so the API's
        own defaults apply, rather than this client pinning a model the
        operator never chose.
        """
        # ast-grep-ignore: no-dict-any - agent_config payload for the Interactions SDK
        agent_config: dict[str, Any] = {"type": "antigravity"}
        if self.antigravity_model:
            agent_config["model"] = self.antigravity_model
        if self.antigravity_max_total_tokens is not None:
            agent_config["max_total_tokens"] = self.antigravity_max_total_tokens
        return agent_config

    # ast-grep-ignore: no-dict-any - environment payload for the Interactions SDK
    async def _build_agent_environment(
        self,
        environment_sources: Sequence[Mapping[str, Any]] | None = None,
        # ast-grep-ignore: no-dict-any - environment payload for the Interactions SDK
    ) -> dict[str, Any] | None:
        """Build the ``environment`` block, or ``None`` to send none.

        Merges the two things that shape a run's sandbox: files mounted into it
        (a delegation's attachments, submit path only — the interactive path
        carries no attachments) and its egress policy, which comes from static
        profile config and so applies to both paths. Credentials named by that
        policy are minted here, per run.
        """
        # ast-grep-ignore: no-dict-any - environment payload for the Interactions SDK
        environment: dict[str, Any] = {}
        if environment_sources:
            environment["sources"] = list(environment_sources)
        if self._antigravity_egress is not None:
            network = await self._antigravity_egress.resolve_network()
            if network is not None:
                environment["network"] = network
        if not environment:
            return None
        return {"type": "remote", **environment}

    def _classify_agent_delegation_error(self, e: Exception) -> Exception:
        """Map an Interactions API exception to the delegation error taxonomy.

        Generic to any Interactions API agent (``agent=`` can be a Deep
        Research model today, or e.g. an Antigravity managed agent in
        future), not just Deep Research — used by the submit/poll/cancel
        primitives for the pollable-delegation path (not the interactive
        streaming path, which raises ``LLMProviderError`` subtypes via
        ``_map_interactions_error`` instead). Duck-types on ``status_code``
        (SDK ``APIStatusError``), mirroring ``_map_interactions_error``'s own
        pattern. Errors this doesn't recognize (including non-HTTP transport
        errors with no ``status_code``) are returned unwrapped — safe, since
        ``task_worker.py``'s default (fail-fast) handling already applies to
        anything that isn't a ``DelegationTransientError``.
        """
        status_code: int | None = getattr(e, "status_code", None)
        if status_code == 404:
            return DelegationTaskNotFoundError(str(e))
        if status_code in {400, 401, 403}:
            return DelegationPermanentError(str(e))
        if status_code == 429 or (status_code is not None and status_code >= 500):
            return DelegationTransientError(str(e))
        return e

    async def start_agent_interaction(
        self,
        messages: Sequence[LLMMessage],
        *,
        previous_interaction_id: str | None = None,
        environment_sources: Sequence[Mapping[str, Any]] | None = None,
    ) -> Interaction:
        """Start an Interactions agent run without waiting for it to finish.

        Used by the pollable-delegation path: submits in the background and
        returns immediately with the interaction's initial (non-terminal)
        state instead of streaming until the whole run completes. Raises the
        generic delegation error taxonomy
        (``DelegationTransientError``/``DelegationPermanentError``/
        ``DelegationTaskNotFoundError``) rather than ``LLMProviderError``.

        Which agent runs follows from ``model_name`` — submitting needs
        agent-specific input shaping (see ``_build_agent_create_kwargs``),
        while ``get_agent_interaction``/``cancel_agent_interaction`` below
        need none, because polling or cancelling by id is the same call
        whatever produced the id.
        """
        create_kwargs = self._build_agent_create_kwargs(
            messages, previous_interaction_id=previous_interaction_id
        )
        # A fresh sandbox with the caller's files mounted into it, under this
        # profile's egress policy. Only the submit path carries sources: the
        # interactive path goes through the provider-agnostic
        # `generate_response_stream`, which has no attachments to mount.
        environment = await self._build_agent_environment(environment_sources)
        if environment is not None:
            create_kwargs["environment"] = environment
        try:
            return cast(
                "Interaction",
                await self.client.aio.interactions.create(
                    **create_kwargs, stream=False
                ),
            )
        except Exception as e:
            raise self._classify_agent_delegation_error(e) from e

    async def get_agent_interaction(self, interaction_id: str) -> Interaction:
        """Fetch the current state of any Interactions API agent run (one poll).

        Generic by design — polling by id needs no agent-specific knowledge.
        Raises the generic delegation error taxonomy on failure (see
        ``start_agent_interaction``).
        """
        try:
            return cast(
                "Interaction",
                await self.client.aio.interactions.get(interaction_id, stream=False),
            )
        except Exception as e:
            raise self._classify_agent_delegation_error(e) from e

    async def cancel_agent_interaction(self, interaction_id: str) -> None:
        """Best-effort cancellation of any Interactions API agent run.

        Generic by design (see ``get_agent_interaction``). Callers (e.g.
        ``InteractionsAgentProcessingService.cancel_async``) are expected to
        swallow any exception this raises, mirroring
        ``RemoteA2AService.cancel_async``.
        """
        await self.client.aio.interactions.cancel(interaction_id)

    async def _generate_agent_interaction_stream(
        self,
        messages: Sequence[LLMMessage],
    ) -> AsyncIterator[LLMStreamEvent]:
        """
        Handle Interactions API agent runs (Deep Research, Antigravity).

        These agents require background execution and polling/streaming via
        interactions.create. Deltas the agent emits that have no text form
        (images today, and any future step type) are skipped rather than
        rendered, so the interactive transcript stays the agent's prose.
        """
        agent_label = self._agent_label
        span = tracer.start_span("llm.provider.agent_interaction")
        telemetry = LLMCallTelemetry(
            span,
            provider="google",
            system="google-genai",
            requested_model=self.model_name,
            messages=messages,
            tools=None,
            tool_choice=None,
            streaming=True,
            operation=agent_label.lower().replace(" ", "_"),
        )

        content_yielded = False
        try:
            create_kwargs = self._build_agent_create_kwargs(messages)
            environment = await self._build_agent_environment()
            if environment is not None:
                create_kwargs["environment"] = environment
            create_kwargs["stream"] = True

            stream = cast(
                "AsyncIterator[Any]",
                await self.client.aio.interactions.create(**create_kwargs),
            )

            interaction_id = None
            last_event_id: str | None = None
            thought_summaries: list[str] = []

            # 3. Process stream
            async for chunk in stream:
                event_type = getattr(chunk, "event_type", None)
                # Track event IDs for potential stream reconnection
                if event_id := getattr(chunk, "event_id", None):
                    last_event_id = event_id

                # Capture Interaction ID
                if event_type in {"interaction.created", "interaction.start"}:
                    interaction_id = chunk.interaction.id
                    logger.info(f"{agent_label} interaction started: {interaction_id}")

                elif event_type in {"step.delta", "content.delta"}:
                    if chunk.delta.type == "text":
                        text_event = LLMStreamEvent(
                            type="content", content=chunk.delta.text
                        )
                        telemetry.observe_event(text_event)
                        yield text_event
                        content_yielded = True
                    elif chunk.delta.type == "thought_summary":
                        thought_text = chunk.delta.content.text
                        thought_summaries.append(thought_text)
                        thought_event = LLMStreamEvent(
                            type="content", content=f"\n*Thinking: {thought_text}*\n"
                        )
                        telemetry.observe_event(thought_event)
                        yield thought_event
                        content_yielded = True
                    elif chunk.delta.type == "image":
                        # Image deltas (e.g. visualization charts) are not yet surfaced as
                        # attachments; skip them so text output is unaffected.
                        logger.debug(
                            f"Ignoring {agent_label} image delta (visualization not yet wired)"
                        )

                elif event_type in {"interaction.completed", "interaction.complete"}:
                    logger.info(f"{agent_label} interaction complete")

                elif event_type == "interaction.status_update":
                    status = getattr(chunk, "status", None) or getattr(
                        getattr(chunk, "interaction", None), "status", None
                    )
                    if status in _INTERACTION_TERMINAL_ERROR_STATUSES:
                        raise ServiceUnavailableError(
                            f"{agent_label} interaction {status}",
                            provider="google",
                            model=self.model_name,
                        )

                elif event_type == "error":
                    error_obj = chunk.error
                    error_code = getattr(error_obj, "code", None)
                    error_message = getattr(error_obj, "message", None) or str(
                        error_obj
                    )
                    logger.error(
                        f"{agent_label} stream error: code={error_code} msg={error_message}"
                    )

                    # Map error code to typed exception
                    exc_class = _STREAM_ERROR_CODE_TO_EXCEPTION.get(
                        error_code or "", LLMProviderError
                    )
                    if exc_class is RateLimitError:
                        typed_exc = RateLimitError(
                            error_message,
                            provider="google",
                            model=self.model_name,
                            retry_after=self._parse_retry_after(error_message),
                        )
                    else:
                        typed_exc = exc_class(
                            error_message,
                            provider="google",
                            model=self.model_name,
                        )

                    raise typed_exc

            # 4. Finalize
            done_metadata: StreamEventMetadata = {}
            if interaction_id:
                done_metadata["provider_metadata"] = GeminiProviderMetadata(
                    interaction_id=interaction_id
                )
            if last_event_id:
                done_metadata["last_event_id"] = last_event_id

            if thought_summaries:
                done_metadata["reasoning_info"] = MessageReasoningInfo(
                    thought_summaries=[{"summary": t} for t in thought_summaries]
                )

            if interaction_id:
                telemetry.record_response_metadata(response_id=interaction_id)
            done_metadata["reasoning_info"] = telemetry.finalize_usage(
                done_metadata.get("reasoning_info")
            )
            telemetry.finish_success({"streaming": True, "metadata": done_metadata})

            yield LLMStreamEvent(type="done", metadata=done_metadata)

        except Exception as e:
            telemetry.finish_error(e)

            # If the exception is already a typed LLMProviderError (e.g. raised by
            # stream error/status handlers above), use it directly.
            if isinstance(e, LLMProviderError):
                typed_error = e
            else:
                typed_error = self._map_interactions_error(e)
            logger.exception(
                f"Google {agent_label} error ({type(typed_error).__name__}): {e}"
            )

            # If no content has been yielded, raise typed exception so
            # RetryingLLMClient can catch it and retry/fallback
            if not content_yielded:
                raise typed_error from e

            # Content already yielded — can't retry, yield error event then done
            yield LLMStreamEvent(
                type="error",
                error=str(e),
                metadata={
                    "error_type": type(typed_error).__name__,
                    "provider": "google",
                    "model": self.model_name,
                },
            )

            # Yield done event to preserve interaction_id for session resumption
            error_done_metadata: StreamEventMetadata = {}
            if interaction_id:
                error_done_metadata["provider_metadata"] = GeminiProviderMetadata(
                    interaction_id=interaction_id
                )
            if last_event_id:
                error_done_metadata["last_event_id"] = last_event_id
            yield LLMStreamEvent(type="done", metadata=error_done_metadata)
        finally:
            span.end()

    async def _generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Internal async generator for streaming responses using Google GenAI."""
        span = tracer.start_span("llm.provider.generate_stream")
        telemetry = LLMCallTelemetry(
            span,
            provider="google",
            system="google-genai",
            requested_model=self.model_name,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            streaming=True,
        )

        content_yielded = False
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
                generation_config.temperature = float(config_params["temperature"])
            if "max_tokens" in config_params:
                generation_config.max_output_tokens = int(config_params["max_tokens"])
            if "top_p" in config_params:
                generation_config.top_p = float(config_params["top_p"])
            if "top_k" in config_params:
                generation_config.top_k = int(config_params["top_k"])

            # Prepare all tools (function tools + grounding tools)
            # Pass tool_choice to respect "none" mode which returns empty list
            all_tools = self._prepare_all_tools(tools, tool_choice)

            # Add tools to config if any are available
            if all_tools:
                generation_config.tools = all_tools
                # Disable automatic function calling so we can manually handle
                # function calls and thought signatures
                generation_config.automatic_function_calling = (
                    types.AutomaticFunctionCallingConfig(disable=True)
                )

            # Configure tool behavior based on tool_choice setting
            if tool_choice == "none":
                # Explicitly configure the model to not call functions
                # This ensures the model returns text even if tools were provided earlier in conversation
                generation_config.tool_config = types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(
                        mode=types.FunctionCallingConfigMode.NONE
                    )
                )
            elif tool_choice == "required":
                # Force the model to call any available tool
                generation_config.tool_config = types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(
                        mode=types.FunctionCallingConfigMode.ANY
                    )
                )
            elif tool_choice and tool_choice not in {"auto", "required"}:
                # Specific tool name: restrict to calling only this tool
                generation_config.tool_config = types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(
                        mode=types.FunctionCallingConfigMode.ANY,
                        allowed_function_names=[tool_choice],
                    )
                )

            # Make streaming API call using generate_content_stream
            stream_response = await self.client.aio.models.generate_content_stream(
                model=self.model_name,
                contents=contents,
                config=generation_config,
            )

            # Track tool calls and thought summaries being accumulated
            accumulated_tool_calls = []
            thought_summaries: list[dict[str, str | int]] = []
            part_index = 0
            # Gemini reports usage on streaming chunks, with the last one
            # carrying the final cumulative counts. Keep the newest seen rather
            # than reading a single chunk, since not every chunk includes it.
            latest_usage_metadata: Any | None = None
            resolved_model: str | None = None
            response_id: str | None = None
            finish_reason: object = None

            # Process stream chunks
            with trace.use_span(span, end_on_exit=False):
                async for chunk in stream_response:  # type: ignore[misc]
                    if getattr(chunk, "usage_metadata", None):
                        latest_usage_metadata = chunk.usage_metadata
                    resolved_model = chunk.model_version or resolved_model
                    response_id = chunk.response_id or response_id
                    finish_reason = _first_finish_reason(chunk) or finish_reason

                    # Extract text content from chunk
                    if hasattr(chunk, "text") and chunk.text:
                        chunk_event = LLMStreamEvent(type="content", content=chunk.text)
                        telemetry.observe_event(chunk_event)
                        yield chunk_event  # noqa: ASYNC119
                        content_yielded = True

                    # Handle candidates structure for more complex responses
                    elif hasattr(chunk, "candidates") and chunk.candidates:
                        for candidate in chunk.candidates:
                            if (
                                hasattr(candidate, "content")
                                and candidate.content
                                and hasattr(candidate.content, "parts")
                                and candidate.content.parts
                                is not None  # Fix None check
                            ):
                                for part in candidate.content.parts:
                                    # Extract thought summary if present (readable for debugging/introspection)
                                    is_thought = (
                                        hasattr(part, "thought") and part.thought
                                    )
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
                                        part_event = LLMStreamEvent(
                                            type="content", content=part.text
                                        )
                                        telemetry.observe_event(part_event)
                                        yield part_event  # noqa: ASYNC119
                                        content_yielded = True

                                    # Accumulate function calls with their thought signatures
                                    if (
                                        hasattr(part, "function_call")
                                        and part.function_call
                                    ):
                                        func_call = part.function_call
                                        if func_call.name:
                                            # Generate a unique ID for the tool call
                                            tool_call_id = (
                                                f"call_{uuid.uuid4().hex[:24]}"
                                            )

                                            # Check if this part also has a thought signature
                                            thought_sig = None
                                            if (
                                                hasattr(part, "thought_signature")
                                                and part.thought_signature
                                            ):
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
                tool_call_event = LLMStreamEvent(
                    type="tool_call", tool_call=tool_call, tool_call_id=tool_call_id
                )
                telemetry.observe_event(tool_call_event)
                yield tool_call_event

            # Signal completion
            done_metadata: StreamEventMetadata = {}

            telemetry.record_response_metadata(
                resolved_model=resolved_model,
                response_id=response_id,
                finish_reason=finish_reason,
            )
            if resolved_model:
                done_metadata["resolved_model"] = resolved_model

            stream_reasoning_info: MessageReasoningInfo | None = None
            if latest_usage_metadata is not None:
                stream_reasoning_info = self._reasoning_info_from_usage_metadata(
                    latest_usage_metadata
                )

            # Add thought summaries to reasoning_info for debugging/introspection
            if thought_summaries:
                if stream_reasoning_info is None:
                    stream_reasoning_info = MessageReasoningInfo()
                stream_reasoning_info["thought_summaries"] = thought_summaries

            done_metadata["reasoning_info"] = telemetry.finalize_usage(
                stream_reasoning_info
            )

            telemetry.finish_success({"streaming": True, "metadata": done_metadata})

            yield LLMStreamEvent(type="done", metadata=done_metadata)

        except Exception as e:
            telemetry.finish_error(e)

            typed_error = self._map_error_to_typed_exception(e)
            logger.exception(
                f"Google GenAI streaming error ({type(typed_error).__name__}): {e}"
            )

            # If no content has been yielded, raise typed exception so
            # RetryingLLMClient can catch it and retry/fallback
            if not content_yielded:
                raise typed_error from e

            # Content already yielded — can't retry, yield error event instead
            yield LLMStreamEvent(
                type="error",
                error=str(e),
                metadata={
                    "error_id": str(e.__class__.__name__),
                    "error_type": type(typed_error).__name__,
                    "provider": "google",
                    "model": self.model_name,
                },
            )
        finally:
            span.end()
