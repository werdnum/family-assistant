"""
Direct Google Generative AI (Gemini) implementation for LLM interactions.
"""

import json
import logging
import uuid
from typing import Any

import aiofiles
from google import genai
from google.genai import types

from family_assistant.llm import (
    LLMInterface,
    LLMOutput,
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
        # Google API requires 'models/' prefix
        self.model_name = (
            f"models/{model}" if not model.startswith("models/") else model
        )
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

    def _convert_messages_to_genai_format(
        self, messages: list[dict[str, Any]]
    ) -> list[Any]:
        """Convert OpenAI-style messages to Gemini format."""
        # Build proper Content objects for the new API
        contents = []

        for msg in messages:
            role = msg["role"]
            content = msg.get("content", "")

            if role == "system":
                # System messages can be included as user messages with a prefix
                contents.append({
                    "role": "user",
                    "parts": [{"text": f"System: {content}"}],
                })
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": content}]})
            elif role == "assistant":
                parts = []

                # Add text content if present
                if content:
                    parts.append({"text": content})

                # Add tool calls if present
                if "tool_calls" in msg and msg["tool_calls"]:
                    for tc in msg["tool_calls"]:
                        if tc.get("type") == "function":
                            func = tc.get("function", {})
                            # Parse arguments if they're a string
                            args = func.get("arguments", {})
                            if isinstance(args, str):
                                args = json.loads(args)

                            parts.append({
                                "functionCall": {"name": func.get("name"), "args": args}
                            })

                if parts:
                    contents.append({"role": "model", "parts": parts})
            elif role == "tool":
                # Handle tool responses
                tool_content = msg.get("content", "")

                # Try to parse the content as JSON
                try:
                    response_data = (
                        json.loads(tool_content)
                        if isinstance(tool_content, str)
                        else tool_content
                    )
                except json.JSONDecodeError:
                    response_data = {"result": tool_content}

                contents.append({
                    "role": "function",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": msg.get("name", "unknown"),
                                "response": response_data,
                            }
                        }
                    ],
                })

        return contents

    def _convert_tools_to_genai_format(self, tools: list[dict[str, Any]]) -> list[Any]:
        """Convert OpenAI-style tools to Gemini format."""
        from google.genai import types

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
                schema_type = prop_def.get("type", "string").upper()
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

    async def generate_response(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Generate response using Google GenAI."""
        try:
            # Convert messages to format expected by new API
            contents = self._convert_messages_to_genai_format(messages)

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
            google_tools = None
            if tools:
                google_tools = self._convert_tools_to_genai_format(tools)

            # Make API call using the client
            # Add tools to config if provided
            if google_tools:
                generation_config.tools = google_tools

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
                    if parts and hasattr(parts[0], "text"):
                        content = parts[0].text

            # Extract tool calls from response
            tool_calls = None
            if hasattr(response, "candidates") and response.candidates:
                candidate = response.candidates[0]
                if (
                    hasattr(candidate, "content")
                    and candidate.content
                    and hasattr(candidate.content, "parts")
                    and candidate.content.parts
                ):
                    found_tool_calls = []
                    for part in candidate.content.parts:
                        if hasattr(part, "function_call") and part.function_call:
                            # Convert Google function call to our format
                            func_call = part.function_call
                            if not func_call.name:
                                logger.warning(
                                    "Received a tool call without a name: %s", func_call
                                )
                                continue
                            tool_call = ToolCallItem(
                                id=f"call_{uuid.uuid4().hex[:24]}",  # Generate ID
                                type="function",
                                function=ToolCallFunction(
                                    name=func_call.name,
                                    arguments=json.dumps(func_call.args),
                                ),
                            )
                            found_tool_calls.append(tool_call)
                    # Only set tool_calls if we found any
                    if found_tool_calls:
                        tool_calls = found_tool_calls

            # Extract usage information if available
            reasoning_info = None
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                usage = response.usage_metadata
                reasoning_info = {
                    "prompt_tokens": getattr(usage, "prompt_token_count", 0),
                    "completion_tokens": getattr(usage, "candidates_token_count", 0),
                    "total_tokens": getattr(usage, "total_token_count", 0),
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
