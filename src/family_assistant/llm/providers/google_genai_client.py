"""
Direct Google Generative AI (Gemini) implementation for LLM interactions.
"""

import logging
from typing import Any

import aiofiles
from google import genai
from google.genai import types

from family_assistant.llm import (
    LLMInterface,
    LLMOutput,
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


class GoogleGenAIClient(LLMInterface):
    """Direct Google Generative AI implementation."""

    def __init__(
        self,
        api_key: str,
        model: str,
        model_parameters: dict[str, dict[str, Any]] | None = None,
        api_base: str | None = None,
        **kwargs: Any,
    ) -> None:
        """
        Initialize Google GenAI client.

        Args:
            api_key: Google API key
            model: Model identifier (e.g., "gemini-2.0-flash-001", "gemini-1.5-pro")
            model_parameters: Pattern-based parameters matching existing config format
            api_base: Optional API base URL for custom endpoints.
            **kwargs: Default parameters for generation
        """
        # Initialize the google-genai client
        if api_base:
            # For custom endpoints, we might need additional configuration
            logger.info(f"Using custom API base: {api_base}")
            # Note: The new API might handle this differently

        self.client = genai.Client(api_key=api_key)
        self.model_name = model
        self.model_parameters = model_parameters or {}
        self.default_kwargs = kwargs

        logger.info(
            f"GoogleGenAIClient initialized for model: {model} with default kwargs: {kwargs}, "
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

    def _convert_messages_to_genai_format(self, messages: list[dict[str, Any]]) -> str:
        """Convert OpenAI-style messages to Gemini format."""
        # For simple cases, join all messages into a single string
        # For more complex cases with tool responses, we'd need to build Content objects

        combined_content = []

        for msg in messages:
            role = msg["role"]
            content = msg.get("content", "")

            if role == "system":
                # Prepend system messages
                combined_content.insert(0, f"System: {content}")
            elif role == "user":
                combined_content.append(f"User: {content}")
            elif role == "assistant":
                combined_content.append(f"Assistant: {content}")
            elif role == "tool":
                # Handle tool responses
                tool_name = msg.get("name", "")
                combined_content.append(f"Tool Response ({tool_name}): {content}")

        return "\n\n".join(combined_content)

    def _convert_tools_to_genai_format(self, tools: list[dict[str, Any]]) -> list[Any]:
        """Convert OpenAI-style tools to Gemini format."""
        # The new API supports passing Python functions directly
        # For now, we'll convert the JSON schema to function signatures
        # This is a simplified implementation - in production, you'd want
        # to dynamically create proper Python functions

        logger.warning(
            "Tool conversion for new google-genai API is simplified - full tool calling may not work as expected"
        )
        return []  # Simplified for now

    async def generate_response(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Generate response using Google GenAI."""
        try:
            # Convert messages to format expected by new API
            content = self._convert_messages_to_genai_format(messages)

            # Build generation config
            config_params = {
                **self.default_kwargs,
                **self._get_model_specific_params(self.model_name),
            }

            # Map common parameters
            generation_config = types.GenerateContentConfig()
            if "temperature" in config_params:
                generation_config.temperature = config_params["temperature"]
            if "max_tokens" in config_params:
                generation_config.max_output_tokens = config_params["max_tokens"]
            if "top_p" in config_params:
                generation_config.top_p = config_params["top_p"]
            if "top_k" in config_params:
                generation_config.top_k = config_params["top_k"]

            # Handle tools if provided
            if tools:
                # Tool handling would need to be implemented based on new API
                logger.warning(
                    "Tool calling not fully implemented for new google-genai API"
                )

            # Make API call using the client
            response = await self.client.aio.models.generate_content(
                model=self.model_name,
                contents=content,
                config=generation_config,
            )

            # Extract content from response
            content = response.text if hasattr(response, "text") else None

            # Tool calls would need to be extracted differently with new API
            tool_calls = None

            # Extract usage information if available
            reasoning_info = None
            if hasattr(response, "usage_metadata"):
                reasoning_info = {
                    "prompt_tokens": getattr(
                        response.usage_metadata, "prompt_token_count", 0
                    ),
                    "completion_tokens": getattr(
                        response.usage_metadata, "candidates_token_count", 0
                    ),
                    "total_tokens": getattr(
                        response.usage_metadata, "total_token_count", 0
                    ),
                }

            return LLMOutput(
                content=content,
                tool_calls=tool_calls,
                reasoning_info=reasoning_info,
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
    ) -> dict[str, Any]:
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
