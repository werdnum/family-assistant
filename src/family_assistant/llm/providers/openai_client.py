"""
Direct OpenAI API implementation for LLM interactions.
"""

import base64
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypedDict, TypeVar, cast

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolAttachment, ToolDefinition

import aiofiles
from openai import AsyncOpenAI
from openai.types.responses import Response
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
from family_assistant.llm.request_buffer import LLMRequestRecord, get_request_buffer

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
T = TypeVar("T", bound=BaseModel)


class _StreamingToolFunction(TypedDict):
    name: str
    arguments: str


class _StreamingToolAccumulator(TypedDict):
    id: str | None
    type: str | None
    function: _StreamingToolFunction


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
        self._is_direct_openai = base_url is None
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

    def _uses_responses_api(self) -> bool:
        """Return whether this direct OpenAI model requires the Responses API."""
        return self._is_direct_openai and self.model.startswith("gpt-5.6-sol")

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
        params: dict[str, object] = {
            "model": self.model,
            "input": self._messages_to_responses_input(messages),
            "include": ["reasoning.encrypted_content"],
            "stream": stream,
        }

        if reasoning_effort is not None:
            params["reasoning"] = {"effort": reasoning_effort}

        for key in (
            "max_tool_calls",
            "parallel_tool_calls",
            "service_tier",
            "store",
            "temperature",
            "top_p",
            "truncation",
            "user",
        ):
            if key in model_params:
                params[key] = model_params[key]
        if "max_tokens" in model_params:
            params["max_output_tokens"] = model_params["max_tokens"]
        if "max_output_tokens" in model_params:
            params["max_output_tokens"] = model_params["max_output_tokens"]

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
                        (
                            {"type": "input_text", "text": part.text}
                            if isinstance(part, TextContentPart)
                            else {
                                "type": "input_image",
                                "image_url": part.image_url["url"],
                            }
                        )
                        for part in message.content
                        if isinstance(part, (TextContentPart, ImageUrlContentPart))
                    ]
                input_items.append({"role": "user", "content": content})
            elif message.role == "system":
                input_items.append({"role": "system", "content": message.content})
            elif message.role == "assistant":
                provider_metadata = message.provider_metadata
                if isinstance(provider_metadata, dict) and isinstance(
                    provider_metadata.get("openai_response_output"), list
                ):
                    input_items.extend(provider_metadata["openai_response_output"])
                else:
                    if message.content:
                        input_items.append({
                            "type": "message",
                            "id": f"msg_{uuid.uuid4().hex}",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": message.content,
                                    "annotations": [],
                                }
                            ],
                        })
                    for tool_call in message.tool_calls or []:
                        input_items.append({
                            "type": "function_call",
                            "call_id": tool_call.id,
                            "name": tool_call.function.name,
                            "arguments": tool_call.function.arguments,
                        })
            elif message.role == "tool":
                input_items.append({
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": message.content,
                })
        return input_items

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

        logger.error(f"OpenAI API error: {e}", exc_info=True)
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

        # Request tracking for diagnostics
        start_time = time.monotonic()
        request_timestamp = datetime.now(UTC)
        request_id = f"openai_{uuid.uuid4().hex[:16]}"

        # Convert messages to dict format for request buffer recording (before try block)
        message_dicts = [message_to_json_dict(msg) for msg in messages]

        try:
            # Process tool attachments before sending
            processed_messages = self._process_tool_messages(list(messages))

            if self._uses_responses_api():
                response = await cast("Any", self.client.responses.create)(
                    **self._build_responses_params(
                        processed_messages, tools, tool_choice, stream=False
                    )
                )
                llm_output = LLMOutput(
                    content=response.output_text or None,
                    tool_calls=self._responses_tool_calls(response),
                    reasoning_info=self._responses_reasoning_info(response),
                    provider_metadata={
                        "openai_response_output": [
                            item.model_dump(mode="json") for item in response.output
                        ]
                    },
                )

                duration_ms = (time.monotonic() - start_time) * 1000
                get_request_buffer().add(
                    LLMRequestRecord(
                        timestamp=request_timestamp,
                        request_id=request_id,
                        model_id=self.model,
                        messages=message_dicts,
                        tools=tools,
                        tool_choice=tool_choice,
                        response=asdict(llm_output),
                        duration_ms=duration_ms,
                        error=None,
                    )
                )
                return llm_output

            # Convert typed messages to dicts for API call
            api_message_dicts = [
                message_to_json_dict(msg) for msg in processed_messages
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

            llm_output = LLMOutput(
                content=content,
                tool_calls=tool_calls,
                reasoning_info=reasoning_info,
            )

            # Record successful request to diagnostics buffer
            duration_ms = (time.monotonic() - start_time) * 1000
            try:
                get_request_buffer().add(
                    LLMRequestRecord(
                        timestamp=request_timestamp,
                        request_id=request_id,
                        model_id=self.model,
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

        except Exception as e:
            # Record failed request to diagnostics buffer
            duration_ms = (time.monotonic() - start_time) * 1000
            try:
                get_request_buffer().add(
                    LLMRequestRecord(
                        timestamp=request_timestamp,
                        request_id=request_id,
                        model_id=self.model,
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
            raise self._map_error_to_typed_exception(e) from e

    async def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
        max_retries: int = 2,
    ) -> T:
        """Generate structured output using OpenAI's native parse endpoint."""
        self._validate_user_input(messages)

        processed_messages = self._process_tool_messages(list(messages))
        api_message_dicts = [message_to_json_dict(msg) for msg in processed_messages]
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
        api_message_dicts = [message_to_json_dict(msg) for msg in processed_messages]
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
        # Request tracking for diagnostics
        start_time = time.monotonic()
        request_timestamp = datetime.now(UTC)
        request_id = f"openai_stream_{uuid.uuid4().hex[:16]}"

        # Convert messages to dict format for request buffer recording (before try block)
        message_dicts = [message_to_json_dict(msg) for msg in messages]

        try:
            # Process tool attachments before sending
            processed_messages = self._process_tool_messages(list(messages))

            if self._uses_responses_api():
                responses_stream_error = False
                async for event in self._generate_responses_stream(
                    processed_messages, tools, tool_choice
                ):
                    if event.type == "error":
                        responses_stream_error = True
                    yield event
                if not responses_stream_error:
                    duration_ms = (time.monotonic() - start_time) * 1000
                    get_request_buffer().add(
                        LLMRequestRecord(
                            timestamp=request_timestamp,
                            request_id=request_id,
                            model_id=self.model,
                            messages=message_dicts,
                            tools=tools,
                            tool_choice=tool_choice,
                            response={"streaming": True},
                            duration_ms=duration_ms,
                            error=None,
                        )
                    )
                return

            # Convert typed messages to dicts for SDK boundary
            api_message_dicts = [
                message_to_json_dict(msg) for msg in processed_messages
            ]

            # Build parameters with defaults, then model-specific overrides
            params = {
                "model": self.model,
                "messages": api_message_dicts,
                "stream": True,  # Enable streaming
                **self.default_kwargs,
                **self._get_model_specific_params(self.model),
            }

            # Add tools if provided
            if tools:
                params["tools"] = tools
                params["tool_choice"] = tool_choice

            # Make streaming API call
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
                if not chunk or not hasattr(chunk, "choices") or not chunk.choices:
                    continue

                delta = chunk.choices[0].delta
                if not delta:
                    continue

                last_chunk_with_usage = chunk

                # Handle content
                if delta.content:
                    yield LLMStreamEvent(type="content", content=delta.content)

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
                    yield LLMStreamEvent(
                        type="tool_call",
                        tool_call=tool_call,
                        tool_call_id=tc_id,
                    )

            # Extract usage information if available
            metadata: StreamEventMetadata = {}
            if (
                last_chunk_with_usage
                and hasattr(last_chunk_with_usage, "usage")
                and last_chunk_with_usage.usage
            ):
                metadata["reasoning_info"] = MessageReasoningInfo(
                    prompt_tokens=last_chunk_with_usage.usage.prompt_tokens,
                    completion_tokens=last_chunk_with_usage.usage.completion_tokens,
                    total_tokens=last_chunk_with_usage.usage.total_tokens,
                )

            # Record successful streaming request to diagnostics buffer
            duration_ms = (time.monotonic() - start_time) * 1000
            try:
                get_request_buffer().add(
                    LLMRequestRecord(
                        timestamp=request_timestamp,
                        request_id=request_id,
                        model_id=self.model,
                        messages=message_dicts,
                        tools=tools,
                        tool_choice=tool_choice,
                        response={"streaming": True, "metadata": metadata},
                        duration_ms=duration_ms,
                        error=None,
                    )
                )
            except Exception as record_err:
                logger.debug(f"Failed to record streaming LLM request: {record_err}")

            # Signal completion
            yield LLMStreamEvent(type="done", metadata=metadata)

        except Exception as e:
            # Record failed streaming request to diagnostics buffer
            duration_ms = (time.monotonic() - start_time) * 1000
            try:
                get_request_buffer().add(
                    LLMRequestRecord(
                        timestamp=request_timestamp,
                        request_id=request_id,
                        model_id=self.model,
                        messages=message_dicts,
                        tools=tools,
                        tool_choice=tool_choice,
                        response=None,
                        duration_ms=duration_ms,
                        error=str(e),
                    )
                )
            except Exception as record_err:
                logger.debug(
                    f"Failed to record streaming LLM request error: {record_err}"
                )

            # Handle errors the same way as non-streaming
            error_message = str(e)

            # Categorize the error type for metadata
            error_type = "unknown"
            if "401" in error_message or "authentication" in error_message.lower():
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

            logger.error(
                f"OpenAI streaming API error ({error_type}): {e}", exc_info=True
            )
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

    async def _generate_responses_stream(
        self,
        messages: Sequence[LLMMessage],
        tools: list["ToolDefinition"] | None,
        tool_choice: str | None,
    ) -> AsyncIterator[LLMStreamEvent]:
        """Stream a Responses API request as the application's common events."""
        stream = await cast("Any", self.client.responses.create)(
            **self._build_responses_params(messages, tools, tool_choice, stream=True)
        )
        response: Any | None = None
        vcr_events = await self._maybe_parse_vcr_stream(stream)
        if vcr_events is not None:
            async for event in self._emit_events_from_responses_dicts(vcr_events):
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
                yield LLMStreamEvent(
                    type="error",
                    error=message or f"OpenAI Responses request {event.type}",
                )
                return
            elif event.type == "error":
                yield LLMStreamEvent(type="error", error=event.message)
                return

        metadata: StreamEventMetadata = {}
        if response is not None:
            reasoning_info = self._responses_reasoning_info(response)
            if reasoning_info:
                metadata["reasoning_info"] = reasoning_info
            metadata["provider_metadata"] = {
                "openai_response_output": [
                    item.model_dump(mode="json") for item in response.output
                ]
            }
        yield LLMStreamEvent(type="done", metadata=metadata)

    async def _emit_events_from_responses_dicts(
        self,
        event_dicts: list[dict[str, object]],
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
                        metadata["reasoning_info"] = MessageReasoningInfo(
                            prompt_tokens=int(usage.get("input_tokens", 0)),
                            completion_tokens=int(usage.get("output_tokens", 0)),
                            total_tokens=int(usage.get("total_tokens", 0)),
                        )
                    output = response.get("output")
                    if isinstance(output, list):
                        metadata["provider_metadata"] = {
                            "openai_response_output": output
                        }
            elif event_type in {"response.failed", "response.incomplete"}:
                response = event.get("response")
                error = response.get("error") if isinstance(response, dict) else None
                message = error.get("message") if isinstance(error, dict) else None
                yield LLMStreamEvent(
                    type="error",
                    error=(
                        message
                        if isinstance(message, str)
                        else f"OpenAI Responses request {event_type}"
                    ),
                )
                return
            elif event_type == "error":
                message = event.get("message")
                yield LLMStreamEvent(
                    type="error",
                    error=message
                    if isinstance(message, str)
                    else "OpenAI stream error",
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

            if chunk.get("usage"):
                last_chunk_with_usage = chunk

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
            metadata["reasoning_info"] = MessageReasoningInfo(
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
            )

        yield LLMStreamEvent(type="done", metadata=metadata)
