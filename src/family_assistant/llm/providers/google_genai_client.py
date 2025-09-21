"""
Direct Google Generative AI (Gemini) implementation for LLM interactions.
"""

import json
import logging
import mimetypes
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolAttachment

import aiofiles
from google import genai
from google.genai import types

from family_assistant.llm import (
    BaseLLMClient,
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


class GoogleGenAIClient(BaseLLMClient):
    """Direct Google Generative AI implementation."""

    def __init__(
        self,
        api_key: str,
        model: str,
        model_parameters: dict[str, dict[str, Any]] | None = None,
        api_base: str | None = None,
        enable_url_context: bool = False,
        enable_google_search: bool = False,
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

        # New configuration options
        self.enable_url_context = enable_url_context
        self.enable_google_search = enable_google_search

        logger.info(
            f"GoogleGenAIClient initialized for model: {model} with default kwargs: {kwargs}, "
            f"model-specific parameters: {model_parameters}, "
            f"URL context: {enable_url_context}, Google Search: {enable_google_search}"
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

    def _supports_multimodal_tools(self) -> bool:
        """Gemini doesn't support multimodal tool responses"""
        return False

    def _create_attachment_injection(
        self, attachment: "ToolAttachment"
    ) -> dict[str, Any]:
        """Create user message with attachment for Gemini"""
        from google.genai import types

        parts: list[dict[str, Any] | types.Part] = [
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

        return {"role": "user", "parts": parts}

    def _convert_messages_to_genai_format(
        self, messages: list[dict[str, Any]]
    ) -> list[Any]:
        """Convert OpenAI-style messages to Gemini format."""
        # First process any tool attachments
        messages = self._process_tool_messages(messages)
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
                # Check if message already has parts (e.g., from attachment injection)
                if "parts" in msg:
                    # Parts can be a mix of dicts and types.Part objects
                    # The API will handle both, so pass them as-is
                    contents.append({"role": "user", "parts": msg["parts"]})
                # Handle both simple string content and multi-part content (text + images)
                elif isinstance(content, str):
                    # Simple text content
                    contents.append({"role": "user", "parts": [{"text": content}]})
                elif isinstance(content, list):
                    # Multi-part content (e.g., text + images)
                    parts = []
                    for part in content:
                        if isinstance(part, dict):
                            if part.get("type") == "text":
                                parts.append({"text": part.get("text", "")})
                            elif part.get("type") == "image_url":
                                # Extract base64 image data
                                image_url = part.get("image_url", {}).get("url", "")
                                if image_url.startswith("data:"):
                                    # Parse data URL: data:image/jpeg;base64,<base64_data>
                                    try:
                                        # Split on comma to separate metadata from data
                                        header, base64_data = image_url.split(",", 1)
                                        # Extract MIME type
                                        mime_type = header.split(";")[0].split(":")[1]

                                        # Decode base64 to bytes - the SDK will re-encode it
                                        import base64

                                        image_bytes = base64.b64decode(base64_data)

                                        # Add as inline data part with raw bytes
                                        parts.append({
                                            "inline_data": {
                                                "mime_type": mime_type,
                                                "data": image_bytes,  # SDK expects bytes, not base64 string
                                            }
                                        })
                                    except Exception as e:
                                        logger.error(
                                            f"Failed to parse image data URL: {e}"
                                        )
                                        # Skip this image part if parsing fails
                                else:
                                    # Non-data URLs should already be converted by ProcessingService
                                    # Log a warning if we still see them here
                                    logger.warning(
                                        f"Non-data URL images not supported by Gemini API: {image_url[:50]}..."
                                    )
                        elif isinstance(part, str):
                            # Fallback for string parts
                            parts.append({"text": part})

                    if parts:
                        contents.append({"role": "user", "parts": parts})
                else:
                    # Fallback for other content types - try to convert to string
                    contents.append({"role": "user", "parts": [{"text": str(content)}]})
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
        from google.genai import types

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
        self, tools: list[dict[str, Any]] | None = None
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

            # Prepare all tools (function tools + grounding tools)
            all_tools = self._prepare_all_tools(tools)

            # Add tools to config if any are available
            if all_tools:
                generation_config.tools = all_tools

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

    def generate_response_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Generate streaming response using Google GenAI."""
        return self._generate_response_stream(messages, tools, tool_choice)

    async def _generate_response_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Internal async generator for streaming responses using Google GenAI."""
        try:
            # Convert messages to format expected by API
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

            # Prepare all tools (function tools + grounding tools)
            all_tools = self._prepare_all_tools(tools)

            # Add tools to config if any are available
            if all_tools:
                generation_config.tools = all_tools
                # TODO: The tool_choice parameter is not currently mapped to Google's API
                # This matches the behavior of the non-streaming implementation

            # Make streaming API call using generate_content_stream
            stream_response = await self.client.aio.models.generate_content_stream(
                model=self.model_name,
                contents=contents,
                config=generation_config,
            )

            # Track tool calls being accumulated
            accumulated_tool_calls = []

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
                                # Handle text parts
                                if hasattr(part, "text") and part.text:
                                    yield LLMStreamEvent(
                                        type="content", content=part.text
                                    )

                                # Accumulate function calls
                                if (
                                    hasattr(part, "function_call")
                                    and part.function_call
                                ):
                                    func_call = part.function_call
                                    if func_call.name:
                                        # Generate a unique ID for the tool call
                                        tool_call_id = f"call_{uuid.uuid4().hex[:24]}"

                                        tool_call = ToolCallItem(
                                            id=tool_call_id,
                                            type="function",
                                            function=ToolCallFunction(
                                                name=func_call.name,
                                                arguments=json.dumps(func_call.args),
                                            ),
                                        )
                                        accumulated_tool_calls.append((
                                            tool_call,
                                            tool_call_id,
                                        ))

            # Emit accumulated tool calls
            for tool_call, tool_call_id in accumulated_tool_calls:
                yield LLMStreamEvent(
                    type="tool_call", tool_call=tool_call, tool_call_id=tool_call_id
                )

            # Signal completion
            # Note: Usage metadata might not be available in streaming mode
            yield LLMStreamEvent(type="done", metadata={})

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
