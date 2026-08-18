"""
Direct OpenAI API implementation for LLM interactions.
"""

import base64
import json
import logging
import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, TypedDict, TypeVar, cast

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolAttachment, ToolDefinition

import aiofiles
from openai import AsyncOpenAI
from openai.types.responses import Response
from opentelemetry import trace
from pydantic import BaseModel, ValidationError

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
    describe_attachment_for_fallback,
)
from family_assistant.llm.messages import (
    ContentPart,
    ImageUrlContentPart,
    LLMMessage,
    MessageReasoningInfo,
    TextContentPart,
    UserMessage,
    message_to_json_dict,
)
from family_assistant.llm.utils.call_telemetry import LLMCallTelemetry

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
tracer = trace.get_tracer(__name__)
T = TypeVar("T", bound=BaseModel)


class _StreamingToolFunction(TypedDict):
    name: str
    arguments: str


class _StreamingToolAccumulator(TypedDict):
    id: str | None
    type: str | None
    function: _StreamingToolFunction


# `llm_parameters` entries that configure this client rather than the request.
# They are stripped before any params reach the OpenAI API.
_CONTROL_PARAMS = frozenset({"use_responses_api"})

# OpenAI proper. Anything else is an OpenAI-*compatible* endpoint, which
# implements Chat Completions but not necessarily Responses.
_OPENAI_API_BASE_URL = "https://api.openai.com/v1"

# What the Responses API will actually accept as a non-text user input part.
# Images become `input_image`; PDFs become `input_file`, which the API parses for
# both text and page images on a vision-capable model. Everything else -- audio
# and video in practice -- has no representation at all: the Responses API takes
# text and images only, and audio input is confined to Chat Completions with a
# dedicated audio model. Those become a text note rather than being forced into
# an `input_image`, which is what the API would reject or misread.
_RESPONSES_IMAGE_MIME_PREFIX = "image/"
_RESPONSES_FILE_MIME_TYPES: dict[str, str] = {"application/pdf": "attachment.pdf"}

# Types worth inlining as bytes on the Responses API: the two it reads directly,
# plus the two a multimodal profile can be handed. Anything else -- an archive, a
# spreadsheet -- gets a description instead, because base64-encoding it into the
# request only to substitute a note would cost the whole payload for nothing.
_HANDOFF_MEDIA_MIME_PREFIXES = ("image/", "audio/", "video/")


def _is_media_mime_type(mime_type: str) -> bool:
    """Whether this type is readable by the Responses API or by the handoff."""
    return (
        mime_type.startswith(_HANDOFF_MEDIA_MIME_PREFIXES)
        or mime_type in _RESPONSES_FILE_MIME_TYPES
    )


class OpenAIClient(BaseLLMClient):
    """Direct OpenAI API implementation."""

    def __init__(
        self,
        api_key: str,
        model: str,
        model_parameters: dict[str, dict[str, object]] | None = None,
        **kwargs: Any,  # noqa: ANN401 # Accepts arbitrary OpenAI API parameters
    ) -> None:
        """
        Initialize OpenAI client.

        Args:
            api_key: OpenAI API key
            model: Model identifier (e.g., "gpt-4", "gpt-3.5-turbo")
            model_parameters: Pattern-based parameters matching existing config format
            **kwargs: Default parameters for completions
        """
        base_url = kwargs.pop("base_url", None)
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        # Read the URL the SDK actually resolved, not the argument we passed.
        # `OPENAI_BASE_URL` in the environment redirects the client without ever
        # reaching this constructor, so trusting the argument would classify a
        # compatible endpoint as direct OpenAI and send it to /responses, which
        # it may not implement.
        self._is_direct_openai = (
            str(self.client.base_url).rstrip("/") == _OPENAI_API_BASE_URL
        )
        self.model = model
        self.model_parameters = model_parameters or {}
        self.default_kwargs = kwargs
        logger.info(
            f"OpenAIClient initialized for model: {model} with default kwargs: {kwargs}, "
            f"model-specific parameters: {model_parameters}"
        )

    def _supports_multimodal_tools(self) -> bool:
        """OpenAI doesn't support multimodal tool responses"""
        return False

    def create_attachment_injection(
        self,
        attachment: "ToolAttachment",
    ) -> UserMessage:
        """Create user message with attachment for OpenAI"""
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
            # Base class now returns UserMessage, just return it directly
            return super().create_attachment_injection(attachment)

        # Handle multimodal content (images) with typed content parts
        # Use ContentPart type to ensure type compatibility with UserMessage
        content_parts: list[ContentPart] = [
            TextContentPart(
                type="text", text="[System: File from previous tool response]"
            )
        ]

        # On the Responses API every binary type is handled by the same
        # MIME-aware conversion the chat path uses: images become `input_image`,
        # PDFs `input_file`, and audio/video a note naming the attachment so the
        # model can hand it to a profile that reads it. Carrying the
        # attachment_id is what makes that note actionable -- this path is how
        # web chat sends PDFs, audio and video (`chat_api.py` routes every
        # non-image upload through `attachment_content`), so without it a web
        # user's PDF became placeholder text on a model that can read PDFs.
        if (
            attachment.content
            and self._uses_responses_api()
            and _is_media_mime_type(attachment.mime_type)
        ):
            b64_data = attachment.get_content_as_base64()
            if b64_data:
                # A type no other provider can represent needs its description in
                # the *text* part as well. `RetryingLLMClient` builds this message
                # from the primary's adapter and hands the same message list to
                # the fallback, and Anthropic drops any data URI that is not an
                # image -- so on a cross-provider fallback the media part vanishes
                # and only this prelude survives. Before this branch these types
                # produced descriptive text, so leaving the prelude bare would
                # have made the fallback strictly less informed than it was.
                # Images are exempt: every configured adapter renders them, so
                # their part is never the thing that disappears.
                if not attachment.mime_type.startswith(_RESPONSES_IMAGE_MIME_PREFIX):
                    content_parts[0] = TextContentPart(
                        type="text",
                        text=describe_attachment_for_fallback(attachment),
                    )
                content_parts.append(
                    ImageUrlContentPart(
                        type="image_url",
                        image_url={
                            "url": f"data:{attachment.mime_type};base64,{b64_data}"
                        },
                        attachment_id=attachment.attachment_id,
                    )
                )
                return UserMessage(content=content_parts)

        if attachment.content and attachment.mime_type.startswith("image/"):
            # Use image_url format for images
            b64_data = attachment.get_content_as_base64()
            if b64_data:
                content_parts.append(
                    ImageUrlContentPart(
                        type="image_url",
                        image_url={
                            "url": f"data:{attachment.mime_type};base64,{b64_data}"
                        },
                    )
                )
        elif attachment.content and attachment.mime_type == "application/pdf":
            # OpenAI models don't officially support PDF attachments in chat completions
            # Fall back to describing the PDF to the model
            size_mb = len(attachment.content) / (1024 * 1024)
            content_parts.append(
                TextContentPart(
                    type="text",
                    text=f"[PDF Document: {attachment.description or 'document.pdf'} "
                    f"({size_mb:.1f}MB) - Content cannot be displayed but was provided "
                    f"as context from the previous tool response]",
                )
            )
        elif attachment.content:
            # Other binary content with data - describe what we have
            size_mb = len(attachment.content) / (1024 * 1024)
            content_parts.append(
                TextContentPart(
                    type="text",
                    text=f"[File content: {attachment.mime_type}, {size_mb:.1f}MB - {attachment.description}. Note: Binary content not accessible to model, text extraction may be needed]",
                )
            )
        elif attachment.file_path:
            # File path reference without content
            content_parts.append(
                TextContentPart(
                    type="text",
                    text=f"[File: {attachment.file_path} - Note: File content not accessible to model]",
                )
            )

        return UserMessage(content=content_parts)

    def _resolve_model_parameters(self, model: str) -> dict[str, object]:
        """Merge every ``llm_parameters`` pattern that matches ``model``."""
        params: dict[str, object] = {}
        for pattern, pattern_params in self.model_parameters.items():
            if pattern in model:
                params.update(pattern_params)
                logger.debug(
                    f"Applied parameters for pattern '{pattern}': {pattern_params}"
                )
        return params

    def _get_model_specific_params(self, model: str) -> dict[str, object]:
        """Model parameters destined for the API, minus local control keys.

        Control keys steer this client rather than the request, so they are
        filtered here -- at the single accessor every request-building path
        already goes through -- instead of being popped at each call site,
        where one missed spot would send an unknown field to the API.
        """
        return {
            key: value
            for key, value in self._resolve_model_parameters(model).items()
            if key not in _CONTROL_PARAMS
        }

    def _uses_responses_api(self) -> bool:
        """Return whether this model should be driven through the Responses API.

        Defaults to **on** for direct OpenAI. Responses is the current API and
        the only one that returns the encrypted reasoning items needed for
        reasoning to survive a tool loop; Chat Completions has no equivalent, so
        anything left on it silently loses reasoning between steps. Every model
        this repo configures accepts the request shape built here, including the
        ``include: ["reasoning.encrypted_content"]`` that non-reasoning models
        simply answer without reasoning items.

        Set ``use_responses_api: false`` in ``llm_parameters`` to pin a specific
        model back to Chat Completions. Defaulting on rather than enumerating
        models means a newly configured model gets reasoning propagation without
        anyone remembering to enrol it -- the failure mode of the previous
        opt-in, and of the model-name prefix before that.

        Restricted to direct OpenAI: the Responses API is not part of the
        OpenAI-compatible surface that OpenRouter and other ``base_url``
        backends implement.
        """
        if not self._is_direct_openai:
            return False
        configured = self._resolve_model_parameters(self.model).get(
            "use_responses_api", True
        )
        if not isinstance(configured, bool):
            raise InvalidRequestError(
                "Invalid use_responses_api configuration for model "
                f"'{self.model}': expected a boolean, got {configured!r}",
                provider="openai",
                model=self.model,
            )
        return configured

    def _build_responses_params(
        self,
        messages: Sequence[LLMMessage],
        tools: list["ToolDefinition"] | None,
        tool_choice: str | None,
        *,
        stream: bool,
    ) -> dict[str, object]:
        """Translate the shared client request shape to the Responses API."""
        model_params = {
            **self.default_kwargs,
            **self._get_model_specific_params(self.model),
        }
        reasoning_effort = model_params.pop("reasoning_effort", None)
        store = model_params.pop("store", False)
        if not isinstance(store, bool):
            raise InvalidRequestError(
                "Invalid OpenAI Responses store configuration: expected a boolean",
                provider="openai",
                model=self.model,
            )
        params: dict[str, object] = {
            "model": self.model,
            "input": self._messages_to_responses_input(messages),
            "include": ["reasoning.encrypted_content"],
            "stream": stream,
        }

        if reasoning_effort is not None:
            params["reasoning"] = {"effort": reasoning_effort}

        params["store"] = store
        max_tokens = model_params.pop("max_tokens", None)
        if max_tokens is not None:
            model_params["max_output_tokens"] = max_tokens
        params.update(model_params)

        if tools:
            params["tools"] = [
                {
                    "type": "function",
                    "name": tool["function"]["name"],
                    "description": tool["function"].get("description"),
                    "parameters": tool["function"].get("parameters", {}),
                    "strict": False,
                }
                for tool in tools
            ]
            params["tool_choice"] = tool_choice

        return params

    @staticmethod
    def _is_replayable_reasoning_item(
        item: dict[str, object], *, originating_response_stored: bool
    ) -> bool:
        """Whether a stored ``reasoning`` output item can be sent back as input.

        The server can resolve an item by ID only when its originating response
        was stored. The current request's ``store`` setting says nothing about
        historical items. Otherwise the encrypted content returned by
        ``include: ["reasoning.encrypted_content"]`` is required. Older metadata
        has no storage marker, so null encrypted content is conservatively
        dropped rather than replayed as an unresolvable item.
        """
        if item.get("type") != "reasoning":
            return True
        if originating_response_stored:
            return True
        return bool(item.get("encrypted_content"))

    @staticmethod
    def _classify_stream_error_type(error_message: str) -> str:
        """Best-effort error classification from a provider message string."""
        lowered = error_message.lower()
        if "401" in error_message or "authentication" in lowered:
            return "authentication"
        if "429" in error_message or "rate limit" in lowered:
            return "rate_limit"
        if "404" in error_message or "model not found" in lowered:
            return "model_not_found"
        if "context length" in lowered or "maximum" in lowered:
            return "context_length"
        if "invalid" in lowered or "400" in error_message:
            return "invalid_request"
        if "connection" in lowered or "network" in lowered:
            return "connection"
        if "timeout" in lowered:
            return "timeout"
        return "unknown"

    def _stream_error_event(
        self,
        error_message: str,
        *,
        error_id: str,
        error_type: str | None = None,
        response: object = None,
    ) -> LLMStreamEvent:
        """Build a stream error event carrying the metadata consumers rely on.

        Without `error_type`/`provider`/`model`, `_map_stream_error_to_exception`
        can only produce a bare ``RuntimeError``, losing the distinction between
        a rate limit and a bad request. The Chat Completions path has always
        attached this; the Responses path did not, which stopped mattering only
        while Responses served a single model.

        A failed run still identifies itself, so when the frame carries a
        response its id and model ride along too -- a turn that failed is the
        one most likely to be taken to the provider, and it cannot be without
        the provider's own id.
        """
        metadata: StreamEventMetadata = {
            "error_id": error_id,
            "error_type": error_type or self._classify_stream_error_type(error_message),
            "provider": "openai",
            "model": self.model,
        }
        response_id = getattr(response, "id", None)
        if isinstance(response_id, str):
            metadata["response_id"] = response_id
        resolved_model = getattr(response, "model", None)
        if isinstance(resolved_model, str):
            metadata["resolved_model"] = resolved_model
        return LLMStreamEvent(type="error", error=error_message, metadata=metadata)

    @staticmethod
    def _to_chat_completions_message(message: LLMMessage) -> dict[str, object | None]:
        """Serialize a message for Chat Completions, dropping internal fields.

        ``provider_metadata`` is our own bookkeeping -- Gemini thought
        signatures, OpenAI Responses output, Anthropic thinking blocks -- and is
        not part of the Chat Completions message schema. ``attachment_id`` on an
        image part is ours too: it exists so the Responses path can name an
        attachment the model cannot read, and Chat Completions has neither that
        handoff nor that field.

        Real OpenAI tolerates the unknown field and answers normally, so this is
        not a live bug there. It is stripped because an OpenAI-*compatible*
        endpoint that validates its input strictly (OpenRouter and the various
        proxy servers this client can be pointed at via ``base_url``) will
        reject the request outright, and because the payload is another vendor's
        private state that the recipient cannot use for anything -- it can only
        bloat the request.

        Not a confidentiality boundary, despite Anthropic thinking blocks
        carrying readable reasoning text rather than an opaque blob: the
        conversation that reasoning was derived from is already being sent to
        whichever provider handles the turn.
        """
        message_dict = message_to_json_dict(message)
        message_dict.pop("provider_metadata", None)
        content = message_dict.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    part.pop("attachment_id", None)
        return message_dict

    def _content_part_to_responses_input(self, part: ContentPart) -> dict[str, object]:
        """Convert one user content part to a Responses input part.

        Unconvertible parts raise rather than being filtered out. Only
        ``attachment`` and ``file_placeholder`` parts can land here, and both
        are supposed to have been resolved upstream -- attachments are converted
        before reaching a provider, and file placeholders exist only for mock
        clients. Silently dropping one would strip content the user supplied and
        surface later as an unrelated "empty input" style failure, which is the
        exact shape of bug the no-silent-failures rule exists to prevent.
        """
        if isinstance(part, TextContentPart):
            return {"type": "input_text", "text": part.text}
        if isinstance(part, ImageUrlContentPart):
            return self._media_part_to_responses_input(
                part.image_url["url"], part.attachment_id
            )
        raise InvalidRequestError(
            f"Cannot send content part of type '{part.type}' to the OpenAI "
            "Responses API; it should have been resolved before reaching the "
            "provider",
            provider="openai",
            model=self.model,
        )

    @staticmethod
    def _data_uri_mime_type(url: str) -> str | None:
        """Return a base64 data URI's MIME type, or None for a plain URL.

        Only the header is sliced. Attachments run to 100 MB, so ~133 MB encoded
        once inlined; slicing past the comma first, or splitting on it, would copy
        the whole payload twice over to read a few leading bytes. `find` scans
        without allocating, and a URI with no comma is not a data URI at all
        rather than one whose MIME type is its entire body.
        """
        prefix = "data:"
        if not url.startswith(prefix):
            return None
        comma = url.find(",", len(prefix))
        if comma == -1:
            return None
        header = url[len(prefix) : comma]
        return header.split(";", 1)[0].strip().lower() or None

    def _media_part_to_responses_input(
        self, url: str, attachment_id: str | None = None
    ) -> dict[str, object]:
        """Render one media part as whatever the Responses API accepts for it.

        The application carries every attachment -- images, PDFs, audio and
        video alike -- as an ``image_url`` part, because that is the shape Gemini
        accepts for all four. The type therefore has to be recovered from the
        data URI here rather than trusted from the part, or a voice note is sent
        as an `input_image` and the API rejects or misreads it.

        Audio and video have no Responses representation, so they become a text
        note naming the type. That keeps the turn intelligible: the model is told
        a file arrived and that it cannot read it, which is something it can act
        on by asking or delegating, rather than being handed a malformed image or
        having the attachment silently disappear.
        """
        mime_type = self._data_uri_mime_type(url)

        # A plain URL carries no type to inspect. Only images are fetchable by
        # the API, and every attachment this application builds is a data URI,
        # so anything else here came from a caller that meant an image.
        if mime_type is None or mime_type.startswith(_RESPONSES_IMAGE_MIME_PREFIX):
            return {"type": "input_image", "image_url": url}

        filename = _RESPONSES_FILE_MIME_TYPES.get(mime_type)
        if filename is not None:
            # `filename` is how the API infers the file type, so it has to carry
            # a matching extension; the original name does not reach this layer.
            return {"type": "input_file", "filename": filename, "file_data": url}

        # Name the attachment when it has an id. Without one the model has no
        # way to refer to the file -- there is no tool that lists a
        # conversation's attachments -- so the difference between this and an
        # actionable turn is whether the id reached us.
        handoff = (
            f"Call delegate_to_service with target_service_id='media_analyst' and "
            f"attachment_ids=['{attachment_id}'] to have a multimodal model "
            f"describe or transcribe it"
            if attachment_id is not None
            else "Ask the user to describe it or to send a transcript"
        )
        return {
            "type": "input_text",
            "text": (
                f"[A {mime_type} attachment was provided. This model reads images "
                f"and PDFs only, so its contents are not part of this turn. "
                f"{handoff}. Do not answer as though you had read it.]"
            ),
        }

    def _messages_to_responses_input(
        self,
        messages: Sequence[LLMMessage],
    ) -> list[dict[str, object]]:
        """Convert application messages to stateless Responses API input items."""
        input_items: list[dict[str, object]] = []
        for message in messages:
            if isinstance(message, (UserMessage,)):
                content: object
                if isinstance(message.content, str):
                    content = message.content
                else:
                    content = [
                        self._content_part_to_responses_input(part)
                        for part in message.content
                    ]
                input_items.append({"role": "user", "content": content})
            elif message.role == "system":
                input_items.append({"role": "system", "content": message.content})
            elif message.role == "assistant":
                provider_metadata = message.provider_metadata
                if isinstance(provider_metadata, dict) and isinstance(
                    provider_metadata.get("openai_response_output"), list
                ):
                    originating_response_stored = (
                        provider_metadata.get("openai_response_stored") is True
                    )
                    for output_item in provider_metadata["openai_response_output"]:
                        if not isinstance(output_item, dict):
                            raise TypeError(
                                "OpenAI Responses output metadata must contain objects"
                            )
                        if not self._is_replayable_reasoning_item(
                            output_item,
                            originating_response_stored=originating_response_stored,
                        ):
                            logger.debug(
                                "Dropping reasoning item %s with no encrypted_content",
                                output_item.get("id"),
                            )
                            continue
                        input_items.append({
                            key: value
                            for key, value in output_item.items()
                            if key != "status"
                        })
                else:
                    if message.content:
                        # No synthesized `id`. A fabricated one matches nothing
                        # server-side, and because it differs on every request
                        # it changes the input prefix each time, defeating
                        # OpenAI's automatic prompt caching for the whole
                        # conversation after this point.
                        input_items.append({
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": message.content,
                                    "annotations": [],
                                }
                            ],
                        })
                    for tool_call in message.tool_calls or []:
                        arguments = tool_call.function.arguments
                        if isinstance(arguments, dict):
                            arguments = json.dumps(arguments)
                        input_items.append({
                            "type": "function_call",
                            "call_id": tool_call.id,
                            "name": tool_call.function.name,
                            "arguments": arguments,
                        })
            elif message.role == "tool":
                input_items.append({
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": message.content,
                })
        return input_items

    @staticmethod
    def _usage_detail_tokens(
        usage: Any,  # noqa: ANN401 - usage arrives as an SDK object or a raw dict
        field: str,
    ) -> int | None:
        """Read a token count out of an OpenAI usage payload's details block.

        The block is `prompt_tokens_details` on Chat Completions and
        `input_tokens_details` on Responses. Both SDK objects and raw dicts turn
        up here: the Responses streaming path and VCR replay hand back parsed
        JSON rather than typed models, and some fields (`cache_write_tokens`) are
        present in API responses but not modelled by the pinned SDK, so they are
        only reachable by name.

        Returns `None` only when the field is absent everywhere. A reported zero
        comes back as `0`, so a known miss stays distinguishable from a payload
        that says nothing.
        """
        for block in ("prompt_tokens_details", "input_tokens_details"):
            details = (
                usage.get(block)
                if isinstance(usage, dict)
                else getattr(usage, block, None)
            )
            if details is None:
                continue
            value = (
                details.get(field)
                if isinstance(details, dict)
                else getattr(details, field, None)
            )
            if value is not None:
                return int(value)
        return None

    @classmethod
    def _cached_prompt_tokens(
        cls,
        usage: Any,  # noqa: ANN401 - usage arrives as an SDK object or a raw dict
    ) -> int | None:
        """Cache-hit prompt tokens. A *subset* of the reported prompt total,
        unlike Anthropic's separate bucket, so it is never added to the total."""
        return cls._usage_detail_tokens(usage, "cached_tokens")

    @classmethod
    def _cache_write_tokens(
        cls,
        usage: Any,  # noqa: ANN401 - usage arrives as an SDK object or a raw dict
    ) -> int | None:
        """Cache-write tokens, reported by the Responses API.

        Chat Completions does not report this; Responses does, even though the
        pinned SDK does not type it. Also a subset of the prompt total rather
        than an extra bucket.
        """
        return cls._usage_detail_tokens(usage, "cache_write_tokens")

    @staticmethod
    def _responses_reasoning_info(response: Response) -> MessageReasoningInfo | None:
        """Extract usage information from a Responses API response."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        reasoning_info = MessageReasoningInfo(
            prompt_tokens=usage.input_tokens,
            completion_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
        )
        details = getattr(usage, "output_tokens_details", None)
        if details and getattr(details, "reasoning_tokens", None) is not None:
            reasoning_info["reasoning_tokens"] = details.reasoning_tokens
        cached = OpenAIClient._cached_prompt_tokens(usage)
        if cached is not None:
            reasoning_info["cached_prompt_tokens"] = cached
        cache_write = OpenAIClient._cache_write_tokens(usage)
        if cache_write is not None:
            reasoning_info["cache_write_tokens"] = cache_write
        return reasoning_info

    @staticmethod
    def _responses_tool_calls(response: Response) -> list[ToolCallItem] | None:
        """Extract function calls from a Responses API response."""
        tool_calls = [
            ToolCallItem(
                id=item.call_id,
                type="function",
                function=ToolCallFunction(name=item.name, arguments=item.arguments),
            )
            for item in response.output
            if item.type == "function_call"
        ]
        return tool_calls or None

    def _map_error_to_typed_exception(self, e: Exception) -> LLMProviderError:
        """Map a raw OpenAI exception to the typed provider error hierarchy."""
        if isinstance(e, LLMProviderError):
            return e
        error_message = str(e)

        if "401" in error_message or "authentication" in error_message.lower():
            return AuthenticationError(
                error_message, provider="openai", model=self.model
            )
        if "429" in error_message or "rate limit" in error_message.lower():
            return RateLimitError(error_message, provider="openai", model=self.model)
        if "404" in error_message or "model not found" in error_message.lower():
            return ModelNotFoundError(
                error_message, provider="openai", model=self.model
            )
        if (
            "context length" in error_message.lower()
            or "maximum" in error_message.lower()
        ):
            return ContextLengthError(
                error_message, provider="openai", model=self.model
            )
        if "invalid" in error_message.lower() or "400" in error_message:
            return InvalidRequestError(
                error_message, provider="openai", model=self.model
            )
        if "connection" in error_message.lower() or "network" in error_message.lower():
            return ProviderConnectionError(
                error_message, provider="openai", model=self.model
            )
        if "timeout" in error_message.lower():
            return ProviderTimeoutError(
                error_message, provider="openai", model=self.model
            )

        logger.error(f"OpenAI API error: {e}", exc_info=e)
        return LLMProviderError(error_message, provider="openai", model=self.model)

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        tools: list["ToolDefinition"] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Generate response using OpenAI API."""
        # Validate user input before processing
        self._validate_user_input(messages)

        with tracer.start_as_current_span("llm.provider.generate") as span:
            telemetry = LLMCallTelemetry(
                span,
                provider="openai",
                system="openai",
                requested_model=self.model,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                streaming=False,
            )
            try:
                return await self._generate_response_inner(
                    messages, tools, tool_choice, telemetry
                )
            except Exception as e:
                telemetry.finish_error(e)
                raise self._map_error_to_typed_exception(e) from e

    async def _generate_response_inner(
        self,
        messages: Sequence[LLMMessage],
        tools: list["ToolDefinition"] | None,
        tool_choice: str | None,
        telemetry: LLMCallTelemetry,
    ) -> LLMOutput:
        """Issue the request and assemble the response, under *telemetry*."""
        # Process tool attachments before sending
        processed_messages = self._process_tool_messages(list(messages))

        if self._uses_responses_api():
            params = self._build_responses_params(
                processed_messages, tools, tool_choice, stream=False
            )
            response = await cast("Any", self.client.responses.create)(**params)
            telemetry.record_response_metadata(
                resolved_model=response.model,
                response_id=response.id,
                finish_reason=response.status,
            )
            llm_output = LLMOutput(
                content=response.output_text or None,
                tool_calls=self._responses_tool_calls(response),
                reasoning_info=self._responses_reasoning_info(response),
                provider_metadata={
                    "openai_response_output": [
                        item.model_dump(mode="json") for item in response.output
                    ],
                    "openai_response_stored": params["store"] is True,
                },
                resolved_model=response.model,
            )

            telemetry.record_output(llm_output)
            if response.status == "completed":
                telemetry.finish_success(asdict(llm_output))
            else:
                # A run that stopped short -- `incomplete` on an output-token
                # limit, or `failed` -- still returns whatever it produced, and
                # this call keeps returning it. Recording it as a success as
                # well would leave the diagnostics saying the turn was fine
                # while the user is looking at a truncated answer.
                telemetry.finish_failure(
                    f"OpenAI Responses request {response.status}",
                    error_type=str(response.status),
                )
            return llm_output

        # Convert typed messages to dicts for API call
        api_message_dicts = [
            self._to_chat_completions_message(msg) for msg in processed_messages
        ]

        # Build parameters with defaults, then model-specific overrides
        params = {
            "model": self.model,
            "messages": api_message_dicts,
            **self.default_kwargs,
            **self._get_model_specific_params(self.model),
        }

        # Add tools if provided
        if tools:
            params["tools"] = tools
            params["tool_choice"] = tool_choice

        # Make API call
        response = await self.client.chat.completions.create(**params)

        # Parse response
        message = response.choices[0].message
        content = message.content

        # Convert tool calls to our format
        tool_calls = None
        if message.tool_calls:
            tool_calls = [
                ToolCallItem(
                    id=tc.id,
                    type=tc.type,
                    function=ToolCallFunction(
                        name=tc.function.name,
                        arguments=tc.function.arguments,
                    ),
                )
                for tc in message.tool_calls
            ]

        # Extract usage information
        reasoning_info: MessageReasoningInfo | None = None
        if response.usage:
            reasoning_info = MessageReasoningInfo(
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
            )

            # Add reasoning tokens if available (for o1 models)
            if hasattr(response.usage, "completion_tokens_details"):
                details = response.usage.completion_tokens_details
                if details and hasattr(details, "reasoning_tokens"):
                    reasoning_info["reasoning_tokens"] = details.reasoning_tokens

            cached = self._cached_prompt_tokens(response.usage)
            if cached is not None:
                reasoning_info["cached_prompt_tokens"] = cached
            cache_write = self._cache_write_tokens(response.usage)
            if cache_write is not None:
                reasoning_info["cache_write_tokens"] = cache_write

        telemetry.record_response_metadata(
            resolved_model=response.model,
            response_id=response.id,
            finish_reason=response.choices[0].finish_reason,
        )

        llm_output = LLMOutput(
            content=content,
            tool_calls=tool_calls,
            reasoning_info=reasoning_info,
            resolved_model=response.model,
        )

        telemetry.record_output(llm_output)
        telemetry.finish_success(asdict(llm_output))

        return llm_output

    async def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
        max_retries: int = 2,
    ) -> T:
        """Generate structured output using OpenAI's native parse endpoint."""
        self._validate_user_input(messages)

        processed_messages = self._process_tool_messages(list(messages))
        api_message_dicts = [
            self._to_chat_completions_message(msg) for msg in processed_messages
        ]
        base_params = {
            "model": self.model,
            "messages": api_message_dicts,
            **self.default_kwargs,
            **self._get_model_specific_params(self.model),
        }

        raw_response: str | None = None
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                response = await self.client.beta.chat.completions.parse(
                    response_format=response_model,
                    **base_params,
                )

                if not response.choices:
                    raise ValueError("LLM returned empty response")

                message = response.choices[0].message
                raw_response = message.content

                if message.parsed is not None:
                    return cast("T", message.parsed)
                if raw_response:
                    return response_model.model_validate_json(
                        self._extract_json_from_response(raw_response)
                    )
                raise ValueError("LLM returned no parsed structured output")

            except (ValidationError, json.JSONDecodeError, ValueError, TypeError) as e:
                last_error = e
                logger.warning(
                    "OpenAI structured output validation failed "
                    f"(attempt {attempt + 1}/{max_retries + 1}): {e}"
                )
                if attempt < max_retries:
                    api_message_dicts.append({
                        "role": "assistant",
                        "content": raw_response or "",
                    })
                    api_message_dicts.append({
                        "role": "user",
                        "content": (
                            "Your previous response did not satisfy the required schema. "
                            f"Error: {e}\n\nPlease try again with valid structured output."
                        ),
                    })
                    raw_response = None
            except Exception as e:
                raise self._map_error_to_typed_exception(e) from e

        raise StructuredOutputError(
            message=f"Failed to generate valid structured output after {max_retries + 1} attempts",
            provider="openai",
            model=self.model,
            raw_response=raw_response,
            validation_error=last_error,
        )

    async def generate_json(
        self,
        messages: Sequence[LLMMessage],
        max_retries: int = 2,
    ) -> JsonObject:
        """Generate a JSON object using OpenAI's native JSON mode."""
        self._validate_user_input(messages)

        messages_with_instruction = self._add_system_instruction(
            messages,
            (
                "You must respond with a valid JSON object. "
                "Respond ONLY with the JSON object and no additional text."
            ),
        )
        processed_messages = self._process_tool_messages(
            list(messages_with_instruction)
        )
        api_message_dicts = [
            self._to_chat_completions_message(msg) for msg in processed_messages
        ]
        base_params = {
            "model": self.model,
            "messages": api_message_dicts,
            "response_format": {"type": "json_object"},
            **self.default_kwargs,
            **self._get_model_specific_params(self.model),
        }

        raw_response: str | None = None
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                response = await self.client.chat.completions.create(**base_params)
                if not response.choices:
                    raise ValueError("LLM returned empty response")

                message = response.choices[0].message
                raw_response = message.content
                if not raw_response:
                    raise ValueError("LLM returned empty content")
                return self._parse_json_object_response(raw_response)

            except (json.JSONDecodeError, TypeError, ValueError) as e:
                last_error = e
                logger.warning(
                    "OpenAI JSON output validation failed "
                    f"(attempt {attempt + 1}/{max_retries + 1}): {e}"
                )
                if attempt < max_retries:
                    api_message_dicts.append({
                        "role": "assistant",
                        "content": raw_response or "",
                    })
                    api_message_dicts.append({
                        "role": "user",
                        "content": (
                            f"Your previous response was not a valid JSON object. Error: {e}\n\n"
                            "Please try again and respond with JSON only."
                        ),
                    })
                    raw_response = None
            except Exception as e:
                raise self._map_error_to_typed_exception(e) from e

        raise StructuredOutputError(
            message=f"Failed to generate valid JSON output after {max_retries + 1} attempts",
            provider="openai",
            model=self.model,
            raw_response=raw_response,
            validation_error=last_error,
        )

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
    ) -> UserMessageDict:
        """
        Format user message with optional file content.

        OpenAI supports images via base64 encoding and text files as text content.
        """
        if not file_path:
            # No file, just text
            return {"role": "user", "content": prompt_text or ""}

        # Determine if file is an image or text
        if mime_type and mime_type.startswith("image/"):
            # Handle image file
            async with aiofiles.open(file_path, "rb") as f:
                image_data = await f.read()

            # Encode to base64
            base64_image = base64.b64encode(image_data).decode("utf-8")
            data_uri = f"data:{mime_type};base64,{base64_image}"

            # Create message with image
            content = []
            if prompt_text:
                content.append({"type": "text", "text": prompt_text})
            content.append({
                "type": "image_url",
                "image_url": {"url": data_uri},
            })

            return {"role": "user", "content": content}

        else:
            # Handle text file
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
        tools: list["ToolDefinition"] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Generate streaming response using OpenAI API."""
        # Validate user input before processing
        self._validate_user_input(messages)
        return self._generate_response_stream(messages, tools, tool_choice)

    async def _generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        tools: list["ToolDefinition"] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Internal async generator for streaming responses."""
        span = tracer.start_span("llm.provider.generate_stream")
        telemetry = LLMCallTelemetry(
            span,
            provider="openai",
            system="openai",
            requested_model=self.model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            streaming=True,
        )

        try:
            # Process tool attachments before sending
            processed_messages = self._process_tool_messages(list(messages))

            if self._uses_responses_api():
                stream_error: LLMStreamEvent | None = None
                with trace.use_span(span, end_on_exit=False):
                    async for event in self._generate_responses_stream(
                        processed_messages, tools, tool_choice
                    ):
                        if event.type == "error":
                            stream_error = event
                            telemetry.record_response_metadata(
                                resolved_model=(event.metadata or {}).get(
                                    "resolved_model"
                                ),
                                response_id=(event.metadata or {}).get("response_id"),
                            )
                            # Recorded before the yield, not after the loop: the
                            # consumer raises on this event, which closes this
                            # generator where it is suspended, so anything left
                            # until afterwards never runs.
                            telemetry.finish_failure(
                                event.error or "OpenAI Responses stream failed",
                                error_type=str(
                                    (event.metadata or {}).get("error_id")
                                    or "responses_stream_error"
                                ),
                            )
                        if event.type == "done" and event.metadata:
                            telemetry.record_response_metadata(
                                resolved_model=event.metadata.get("resolved_model"),
                                response_id=event.metadata.get("response_id"),
                                finish_reason=event.metadata.get("finish_reason"),
                            )
                            telemetry.record_usage(event.metadata.get("reasoning_info"))
                        telemetry.observe_event(event)
                        yield event  # noqa: ASYNC119
                if stream_error is None:
                    telemetry.finish_success({"streaming": True})
                return

            # Convert typed messages to dicts for SDK boundary
            api_message_dicts = [
                self._to_chat_completions_message(msg) for msg in processed_messages
            ]

            # Build parameters with defaults, then model-specific overrides
            params = {
                "model": self.model,
                "messages": api_message_dicts,
                "stream": True,  # Enable streaming
                # Streaming responses carry no usage unless it is requested, so
                # without this the whole turn reports no tokens at all -- prompt
                # cache hits included. Only sent to the official API: an
                # OpenAI-compatible endpoint that rejects unknown parameters
                # would fail the request outright. Operators whose endpoint does
                # support it can opt in through model_parameters, since those
                # are merged below and win over this default.
                **(
                    {"stream_options": {"include_usage": True}}
                    if self._is_direct_openai
                    else {}
                ),
                **self.default_kwargs,
                **self._get_model_specific_params(self.model),
            }

            # Add tools if provided
            if tools:
                params["tools"] = tools
                params["tool_choice"] = tool_choice

            # Make streaming API call
            with trace.use_span(span, end_on_exit=False):
                stream = await self.client.chat.completions.create(**params)

            # VCR replays flatten streaming bodies into a single line with a
            # custom marker. The OpenAI SDK doesn't decode that format, so we
            # manually parse and emit events when detected.
            vcr_chunks = await self._maybe_parse_vcr_stream(stream)
            if vcr_chunks is not None:
                async for event in self._emit_events_from_chunk_dicts(vcr_chunks):
                    yield event
                return

            # Track current tool call being built
            current_tool_calls: dict[int, _StreamingToolAccumulator] = {}
            chunk: Any | None = None
            last_chunk_with_usage: Any | None = None

            async for chunk in stream:
                # The usage chunk arrives last and carries an empty `choices`
                # list, so it has to be captured before the guards below skip it.
                if chunk is not None and getattr(chunk, "usage", None):
                    last_chunk_with_usage = chunk
                if chunk is not None:
                    telemetry.record_response_metadata(
                        resolved_model=getattr(chunk, "model", None),
                        response_id=getattr(chunk, "id", None),
                        finish_reason=(
                            chunk.choices[0].finish_reason if chunk.choices else None
                        ),
                    )

                if not chunk or not hasattr(chunk, "choices") or not chunk.choices:
                    continue

                delta = chunk.choices[0].delta
                if not delta:
                    continue

                # Handle content
                if delta.content:
                    content_event = LLMStreamEvent(
                        type="content", content=delta.content
                    )
                    telemetry.observe_event(content_event)
                    yield content_event

                # Handle tool calls
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        tc_id = tc_delta.id
                        tc_type = tc_delta.type
                        func_name = tc_delta.function.name if tc_delta.function else ""
                        func_args = (
                            tc_delta.function.arguments if tc_delta.function else ""
                        )

                        if idx not in current_tool_calls:
                            current_tool_calls[idx] = {
                                "id": tc_id,
                                "type": tc_type,
                                "function": {
                                    "name": "",
                                    "arguments": "",
                                },
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
                tc_id = tc_data["id"]
                if tc_data["function"]["name"] and tc_id:
                    tool_call = ToolCallItem(
                        id=tc_id,
                        type=tc_data["type"] or "function",
                        function=ToolCallFunction(
                            name=tc_data["function"]["name"],
                            arguments=tc_data["function"]["arguments"] or "{}",
                        ),
                    )
                    tool_call_event = LLMStreamEvent(
                        type="tool_call",
                        tool_call=tool_call,
                        tool_call_id=tc_id,
                    )
                    telemetry.observe_event(tool_call_event)
                    yield tool_call_event

            # Extract usage information if available
            metadata: StreamEventMetadata = {}
            if (
                last_chunk_with_usage
                and hasattr(last_chunk_with_usage, "usage")
                and last_chunk_with_usage.usage
            ):
                stream_usage = last_chunk_with_usage.usage
                stream_reasoning_info = MessageReasoningInfo(
                    prompt_tokens=stream_usage.prompt_tokens,
                    completion_tokens=stream_usage.completion_tokens,
                    total_tokens=stream_usage.total_tokens,
                )
                cached = self._cached_prompt_tokens(stream_usage)
                if cached is not None:
                    stream_reasoning_info["cached_prompt_tokens"] = cached
                cache_write = self._cache_write_tokens(stream_usage)
                if cache_write is not None:
                    stream_reasoning_info["cache_write_tokens"] = cache_write
                # Mirror the non-streaming path: reasoning models report this
                # under completion_tokens_details, and enabling include_usage is
                # what makes it reachable on a streamed turn at all.
                completion_details = getattr(
                    stream_usage, "completion_tokens_details", None
                )
                reasoning_tokens = getattr(completion_details, "reasoning_tokens", None)
                if reasoning_tokens is not None:
                    stream_reasoning_info["reasoning_tokens"] = reasoning_tokens
                metadata["reasoning_info"] = stream_reasoning_info
                telemetry.record_usage(stream_reasoning_info)

            if telemetry.resolved_model:
                metadata["resolved_model"] = telemetry.resolved_model
            telemetry.finish_success({"streaming": True, "metadata": metadata})

            # Signal completion
            yield LLMStreamEvent(type="done", metadata=metadata)

        except Exception as e:
            telemetry.finish_error(e)

            # Handle errors the same way as non-streaming
            error_message = str(e)

            # Categorize the error type for metadata
            error_type = "unknown"
            if isinstance(e, InvalidRequestError):
                error_type = "invalid_request"
            elif "401" in error_message or "authentication" in error_message.lower():
                error_type = "authentication"
            elif "429" in error_message or "rate limit" in error_message.lower():
                error_type = "rate_limit"
            elif "404" in error_message or "model not found" in error_message.lower():
                error_type = "model_not_found"
            elif (
                "context length" in error_message.lower()
                or "maximum" in error_message.lower()
            ):
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

            logger.exception(f"OpenAI streaming API error ({error_type}): {e}")
            yield LLMStreamEvent(
                type="error",
                error=error_message,
                metadata={
                    "error_id": str(e.__class__.__name__),
                    "error_type": error_type,
                    "provider": "openai",
                    "model": self.model,
                },
            )
        finally:
            span.end()

    async def _generate_responses_stream(
        self,
        messages: Sequence[LLMMessage],
        tools: list["ToolDefinition"] | None,
        tool_choice: str | None,
    ) -> AsyncIterator[LLMStreamEvent]:
        """Stream a Responses API request as the application's common events."""
        params = self._build_responses_params(messages, tools, tool_choice, stream=True)
        stream = await cast("Any", self.client.responses.create)(**params)
        originating_response_stored = params["store"] is True
        response: Any | None = None
        vcr_events = await self._maybe_parse_vcr_stream(stream)
        if vcr_events is not None:
            async for event in self._emit_events_from_responses_dicts(
                vcr_events,
                originating_response_stored=originating_response_stored,
            ):
                yield event
            return

        async for event in stream:
            if event.type == "response.output_text.delta":
                yield LLMStreamEvent(type="content", content=event.delta)
            elif (
                event.type == "response.output_item.done"
                and event.item.type == "function_call"
            ):
                tool_call = ToolCallItem(
                    id=event.item.call_id,
                    type="function",
                    function=ToolCallFunction(
                        name=event.item.name,
                        arguments=event.item.arguments,
                    ),
                )
                yield LLMStreamEvent(
                    type="tool_call",
                    tool_call=tool_call,
                    tool_call_id=tool_call.id,
                )
            elif event.type == "response.completed":
                response = event.response
            elif event.type in {"response.failed", "response.incomplete"}:
                error = getattr(event.response, "error", None)
                message = getattr(error, "message", None)
                yield self._stream_error_event(
                    message or f"OpenAI Responses request {event.type}",
                    error_id=event.type,
                    response=event.response,
                )
                return
            elif event.type == "error":
                yield self._stream_error_event(event.message, error_id="response.error")
                return

        metadata: StreamEventMetadata = {}
        if response is not None:
            reasoning_info = self._responses_reasoning_info(response)
            if reasoning_info:
                metadata["reasoning_info"] = reasoning_info
            if response.model:
                metadata["resolved_model"] = response.model
            # Diagnostics only: the wrapper reads these off the terminal event
            # because it never sees the Response object itself, and without the
            # provider's own id a slow or failed turn cannot be matched against
            # OpenAI's logs.
            if response.id:
                metadata["response_id"] = response.id
            if response.status:
                metadata["finish_reason"] = response.status
            metadata["provider_metadata"] = {
                "openai_response_output": [
                    item.model_dump(mode="json") for item in response.output
                ],
                "openai_response_stored": originating_response_stored,
            }
        yield LLMStreamEvent(type="done", metadata=metadata)

    async def _emit_events_from_responses_dicts(
        self,
        event_dicts: list[dict[str, object]],
        *,
        originating_response_stored: bool,
    ) -> AsyncIterator[LLMStreamEvent]:
        """Emit common stream events from VCR-replayed Responses API frames."""
        metadata: StreamEventMetadata = {}
        for event in event_dicts:
            event_type = event.get("type")
            if event_type == "response.output_text.delta":
                delta = event.get("delta")
                if isinstance(delta, str):
                    yield LLMStreamEvent(type="content", content=delta)
            elif event_type == "response.output_item.done":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "function_call":
                    call_id = item.get("call_id")
                    name = item.get("name")
                    arguments = item.get("arguments")
                    if (
                        isinstance(call_id, str)
                        and isinstance(name, str)
                        and isinstance(arguments, str)
                    ):
                        tool_call = ToolCallItem(
                            id=call_id,
                            type="function",
                            function=ToolCallFunction(name=name, arguments=arguments),
                        )
                        yield LLMStreamEvent(
                            type="tool_call",
                            tool_call=tool_call,
                            tool_call_id=call_id,
                        )
            elif event_type == "response.completed":
                response = event.get("response")
                if isinstance(response, dict):
                    usage = response.get("usage")
                    if isinstance(usage, dict):
                        responses_reasoning_info = MessageReasoningInfo(
                            prompt_tokens=int(usage.get("input_tokens", 0)),
                            completion_tokens=int(usage.get("output_tokens", 0)),
                            total_tokens=int(usage.get("total_tokens", 0)),
                        )
                        cached = self._cached_prompt_tokens(usage)
                        if cached is not None:
                            responses_reasoning_info["cached_prompt_tokens"] = cached
                        cache_write = self._cache_write_tokens(usage)
                        if cache_write is not None:
                            responses_reasoning_info["cache_write_tokens"] = cache_write
                        metadata["reasoning_info"] = responses_reasoning_info
                    output = response.get("output")
                    if isinstance(output, list):
                        metadata["provider_metadata"] = {
                            "openai_response_output": output,
                            "openai_response_stored": originating_response_stored,
                        }
            elif event_type in {"response.failed", "response.incomplete"}:
                response = event.get("response")
                error = response.get("error") if isinstance(response, dict) else None
                message = error.get("message") if isinstance(error, dict) else None
                yield self._stream_error_event(
                    message
                    if isinstance(message, str)
                    else f"OpenAI Responses request {event_type}",
                    error_id=str(event_type),
                )
                return
            elif event_type == "error":
                message = event.get("message")
                yield self._stream_error_event(
                    message if isinstance(message, str) else "OpenAI stream error",
                    error_id="response.error",
                )
                return
        yield LLMStreamEvent(type="done", metadata=metadata)

    async def _maybe_parse_vcr_stream(
        self,
        stream: Any,  # noqa: ANN401 - OpenAI AsyncStream[ChatCompletionChunk] erased by VCR replay
        # ast-grep-ignore: no-dict-any - VCR replay parses raw JSON SSE frames into unstructured dicts
    ) -> list[dict[str, Any]] | None:
        """
        Detect and parse VCR-recorded streaming responses.

        VCR serializes SSE bodies by replacing newlines with a custom marker,
        which the OpenAI SDK cannot decode. When we detect this marker, we
        parse the entire response body into chunk dictionaries and return
        them for manual event emission.
        """
        # Avoid interfering with real streaming outside of tests
        if (
            not os.getenv("PYTEST_CURRENT_TEST")
            and os.getenv("LLM_RECORD_MODE") != "replay"
        ):
            return None

        response = getattr(stream, "response", None)
        if response is None:
            return None

        try:
            raw_body = await response.aread()
        except Exception:
            return None

        text_body = raw_body.decode()
        magic_token = "#magic___^_^___line"
        if magic_token not in text_body:
            return None

        # Replace the marker with real newlines to reconstruct SSE frames
        normalized = text_body.replace(f"{magic_token} ", "\n")

        # ast-grep-ignore: no-dict-any - VCR replay parses raw JSON SSE frames into unstructured dicts
        chunks: list[dict[str, Any]] = []
        for raw_block in normalized.split("\n\n"):
            data_line = next(
                (
                    line.removeprefix("data:").strip()
                    for line in raw_block.splitlines()
                    if line.startswith("data:")
                ),
                None,
            )
            if data_line is None:
                continue
            if data_line == "[DONE]":
                break

            try:
                chunk = json.loads(data_line)
                if isinstance(chunk, dict):
                    chunks.append(chunk)
            except json.JSONDecodeError:
                logger.debug("Skipping unparsable VCR SSE block: %s", data_line)

        return chunks

    async def _emit_events_from_chunk_dicts(
        self,
        chunk_dicts: list[dict[str, object]],
    ) -> AsyncIterator[LLMStreamEvent]:
        """Emit stream events from pre-parsed chunk dictionaries."""
        current_tool_calls: dict[int, _StreamingToolAccumulator] = {}
        # ast-grep-ignore: no-dict-any - VCR replay chunk is parsed JSON, not a typed SDK object
        last_chunk_with_usage: dict[str, Any] | None = None

        for chunk in chunk_dicts:
            # The terminal usage chunk carries an empty `choices` list, so it has
            # to be captured before the guard below skips it -- same shape as the
            # live-stream path.
            if chunk.get("usage"):
                last_chunk_with_usage = chunk

            choices = chunk.get("choices") or []
            if not choices or not isinstance(choices, list):
                continue

            first_choice = choices[0]
            if not isinstance(first_choice, dict):
                continue

            delta = first_choice.get("delta") or {}
            if not isinstance(delta, dict):
                continue
            if delta.get("content"):
                yield LLMStreamEvent(type="content", content=delta["content"])

            tool_calls = delta.get("tool_calls") or []
            for tc_delta in tool_calls:
                idx = tc_delta.get("index", 0)
                tc_id = tc_delta.get("id")
                tc_type = tc_delta.get("type")
                function = tc_delta.get("function") or {}
                func_name = function.get("name", "")
                func_args = function.get("arguments", "")

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

        for tc_data in current_tool_calls.values():
            tc_id = tc_data["id"]
            if tc_data["function"]["name"] and tc_id:
                tool_call = ToolCallItem(
                    id=tc_id,
                    type=tc_data["type"] or "function",
                    function=ToolCallFunction(
                        name=tc_data["function"]["name"],
                        arguments=tc_data["function"]["arguments"] or "{}",
                    ),
                )
                yield LLMStreamEvent(
                    type="tool_call",
                    tool_call=tool_call,
                    tool_call_id=tc_id,
                )

        metadata: StreamEventMetadata = {}
        if last_chunk_with_usage:
            usage = last_chunk_with_usage.get("usage") or {}
            replay_reasoning_info = MessageReasoningInfo(
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
            )
            cached = self._cached_prompt_tokens(usage)
            if cached is not None:
                replay_reasoning_info["cached_prompt_tokens"] = cached
            cache_write = self._cache_write_tokens(usage)
            if cache_write is not None:
                replay_reasoning_info["cache_write_tokens"] = cache_write
            metadata["reasoning_info"] = replay_reasoning_info

        yield LLMStreamEvent(type="done", metadata=metadata)
