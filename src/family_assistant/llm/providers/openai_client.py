"""
Direct OpenAI API implementation for LLM interactions.
"""

import base64
import logging
from collections.abc import AsyncIterator
from typing import Any

import aiofiles
from openai import AsyncOpenAI

from family_assistant.llm import (
    LLMInterface,
    LLMOutput,
    LLMStreamEvent,
    ToolCallFunction,
    ToolCallItem,
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


class OpenAIClient(LLMInterface):
    """Direct OpenAI API implementation."""

    def __init__(
        self,
        api_key: str,
        model: str,
        model_parameters: dict[str, dict[str, Any]] | None = None,
        **kwargs: Any,
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

    def _get_model_specific_params(self, model: str) -> dict[str, Any]:
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
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Generate response using OpenAI API."""
        try:
            # Build parameters with defaults, then model-specific overrides
            params = {
                "model": self.model,
                "messages": messages,
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
    ) -> dict[str, Any]:
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
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Generate streaming response using OpenAI API."""
        return self._generate_response_stream(messages, tools, tool_choice)

    async def _generate_response_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Internal async generator for streaming responses."""
        try:
            # Build parameters with defaults, then model-specific overrides
            params = {
                "model": self.model,
                "messages": messages,
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

            # Track current tool call being built
            current_tool_calls: dict[int, dict[str, Any]] = {}
            chunk = None  # Initialize for pylint

            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if not delta:
                    continue

                # Stream content chunks
                if delta.content:
                    yield LLMStreamEvent(type="content", content=delta.content)

                # Handle tool calls
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index

                        # Initialize new tool call
                        if idx not in current_tool_calls:
                            current_tool_calls[idx] = {
                                "id": tc_delta.id,
                                "type": tc_delta.type,
                                "function": {
                                    "name": tc_delta.function.name
                                    if tc_delta.function
                                    else "",
                                    "arguments": "",
                                },
                            }

                        # Accumulate function arguments
                        if tc_delta.function and tc_delta.function.arguments:
                            current_tool_calls[idx]["function"]["arguments"] += (
                                tc_delta.function.arguments
                            )

                        # Continue accumulating tool call data
                        # We'll emit complete tool calls after the stream ends

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
            metadata = {}
            if chunk and hasattr(chunk, "usage") and chunk.usage:
                metadata["reasoning_info"] = {
                    "prompt_tokens": chunk.usage.prompt_tokens,
                    "completion_tokens": chunk.usage.completion_tokens,
                    "total_tokens": chunk.usage.total_tokens,
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
