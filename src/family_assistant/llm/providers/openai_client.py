"""
Direct OpenAI API implementation for LLM interactions.
"""

import base64
import json
import logging
import os
from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolAttachment

import aiofiles
from openai import AsyncOpenAI

from family_assistant.llm import (
    BaseLLMClient,
    LLMOutput,
    LLMStreamEvent,
    ToolCallFunction,
    ToolCallItem,
)
from family_assistant.llm.messages import (
    ContentPart,
    ImageUrlContentPart,
    LLMMessage,
    TextContentPart,
    UserMessage,
    message_to_json_dict,
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

StreamingMetadata = dict[str, object]


logger = logging.getLogger(__name__)


class OpenAIClient(BaseLLMClient):
    """Direct OpenAI API implementation."""

    def __init__(
        self,
        api_key: str,
        model: str,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
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
        self.client = AsyncOpenAI(api_key=api_key)
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

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, object]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Generate response using OpenAI API."""
        # Validate user input before processing
        self._validate_user_input(messages)

        try:
            # Process tool attachments before sending
            processed_messages = self._process_tool_messages(list(messages))

            # Convert typed messages to dicts
            message_dicts = [message_to_json_dict(msg) for msg in processed_messages]

            # Build parameters with defaults, then model-specific overrides
            params = {
                "model": self.model,
                "messages": message_dicts,
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
            reasoning_info = None
            if response.usage:
                reasoning_info = {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                }

                # Add reasoning tokens if available (for o1 models)
                if hasattr(response.usage, "completion_tokens_details"):
                    details = response.usage.completion_tokens_details
                    if details and hasattr(details, "reasoning_tokens"):
                        reasoning_info["reasoning_tokens"] = details.reasoning_tokens

            return LLMOutput(
                content=content,
                tool_calls=tool_calls,
                reasoning_info=reasoning_info,
            )

        except Exception as e:
            # Map OpenAI exceptions to our exception hierarchy
            error_message = str(e)

            if "401" in error_message or "authentication" in error_message.lower():
                raise AuthenticationError(
                    error_message, provider="openai", model=self.model
                ) from e
            elif "429" in error_message or "rate limit" in error_message.lower():
                raise RateLimitError(
                    error_message, provider="openai", model=self.model
                ) from e
            elif "404" in error_message or "model not found" in error_message.lower():
                raise ModelNotFoundError(
                    error_message, provider="openai", model=self.model
                ) from e
            elif (
                "context length" in error_message.lower()
                or "maximum" in error_message.lower()
            ):
                raise ContextLengthError(
                    error_message, provider="openai", model=self.model
                ) from e
            elif "invalid" in error_message.lower() or "400" in error_message:
                raise InvalidRequestError(
                    error_message, provider="openai", model=self.model
                ) from e
            elif (
                "connection" in error_message.lower()
                or "network" in error_message.lower()
            ):
                raise ProviderConnectionError(
                    error_message, provider="openai", model=self.model
                ) from e
            elif "timeout" in error_message.lower():
                raise ProviderTimeoutError(
                    error_message, provider="openai", model=self.model
                ) from e
            else:
                logger.error(f"OpenAI API error: {e}", exc_info=True)
                raise LLMProviderError(
                    error_message, provider="openai", model=self.model
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
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, object]] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Generate streaming response using OpenAI API."""
        # Validate user input before processing
        self._validate_user_input(messages)
        return self._generate_response_stream(messages, tools, tool_choice)

    async def _generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, object]] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Internal async generator for streaming responses."""
        try:
            # Process tool attachments before sending
            processed_messages = self._process_tool_messages(list(messages))

            # Convert typed messages to dicts for SDK boundary
            message_dicts = [message_to_json_dict(msg) for msg in processed_messages]

            # Build parameters with defaults, then model-specific overrides
            params = {
                "model": self.model,
                "messages": message_dicts,
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
            # ast-grep-ignore: no-dict-any - Streaming accumulator structure
            current_tool_calls: dict[int, dict[str, Any]] = {}
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
                if tc_data["function"]["name"] and tc_data["id"]:
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

            # Extract usage information if available
            metadata: StreamingMetadata = {}
            if (
                last_chunk_with_usage
                and hasattr(last_chunk_with_usage, "usage")
                and last_chunk_with_usage.usage
            ):
                metadata["reasoning_info"] = {
                    "prompt_tokens": last_chunk_with_usage.usage.prompt_tokens,
                    "completion_tokens": last_chunk_with_usage.usage.completion_tokens,
                    "total_tokens": last_chunk_with_usage.usage.total_tokens,
                }

            # Signal completion
            yield LLMStreamEvent(type="done", metadata=metadata)

        except Exception as e:
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

    async def _maybe_parse_vcr_stream(
        self,
        stream: Any,  # noqa: ANN401  # OpenAI AsyncStream has dynamic types
        # ast-grep-ignore: no-dict-any - VCR chunk payload mirrors provider schema
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

        # ast-grep-ignore: no-dict-any - VCR chunk payload mirrors provider schema
        chunks: list[dict[str, Any]] = []
        for raw_block in normalized.split("\n\n"):
            block = raw_block.strip()
            if not block.startswith("data:"):
                continue

            data_str = block.removeprefix("data:").strip()
            if data_str == "[DONE]":
                break

            try:
                chunk = json.loads(data_str)
                if isinstance(chunk, dict):
                    chunks.append(chunk)
            except json.JSONDecodeError:
                logger.debug("Skipping unparsable VCR SSE block: %s", data_str)

        return chunks

    # ast-grep-ignore: no-dict-any - Streaming chunk payload mirrors provider schema
    async def _emit_events_from_chunk_dicts(
        self,
        chunk_dicts: list[dict[str, object]],
    ) -> AsyncIterator[LLMStreamEvent]:
        """Emit stream events from pre-parsed chunk dictionaries."""
        # ast-grep-ignore: no-dict-any - Streaming chunk payload mirrors provider schema
        current_tool_calls: dict[int, dict[str, Any]] = {}
        # ast-grep-ignore: no-dict-any - Streaming chunk payload mirrors provider schema
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
            if tc_data["function"]["name"] and tc_data["id"]:
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

        metadata: StreamingMetadata = {}
        if last_chunk_with_usage:
            usage = last_chunk_with_usage.get("usage") or {}
            metadata["reasoning_info"] = {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            }

        yield LLMStreamEvent(type="done", metadata=metadata)
