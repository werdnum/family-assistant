"""
Direct Google Generative AI (Gemini) implementation for LLM interactions.
"""

import json
import logging
from typing import Any

import aiofiles
import google.generativeai as genai
from google.generativeai.types import (
    HarmBlockThreshold,
    HarmCategory,
)

from ..base import (
    AuthenticationError,
    ContextLengthError,
    InvalidRequestError,
    LLMInterface,
    LLMOutput,
    LLMProviderError,
    ModelNotFoundError,
    ProviderConnectionError,
    ProviderTimeoutError,
    RateLimitError,
    ToolCallFunction,
    ToolCallItem,
)

logger = logging.getLogger(__name__)


class GoogleGenAIClient(LLMInterface):
    """Direct Google Generative AI implementation."""

    def __init__(
        self,
        api_key: str,
        model: str,
        model_parameters: dict[str, dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> None:
        """
        Initialize Google GenAI client.

        Args:
            api_key: Google API key
            model: Model identifier (e.g., "gemini-pro", "gemini-pro-vision")
            model_parameters: Pattern-based parameters matching existing config format
            **kwargs: Default parameters for generation
        """
        genai.configure(api_key=api_key)
        self.model_name = model
        self.model_parameters = model_parameters or {}
        self.default_kwargs = kwargs

        # Extract safety settings if provided
        safety_settings = kwargs.pop("safety_settings", None)
        if not safety_settings:
            # Use permissive safety settings by default
            safety_settings = {
                HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            }

        self.model = genai.GenerativeModel(
            model_name=model,
            safety_settings=safety_settings,
        )

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
    ) -> list[dict[str, Any]]:
        """Convert OpenAI-style messages to Gemini format."""
        genai_messages = []

        for msg in messages:
            role = msg["role"]
            content = msg.get("content", "")

            # Map roles
            if role == "system":
                # Gemini doesn't have system role, prepend to first user message
                if genai_messages and genai_messages[0]["role"] == "user":
                    genai_messages[0]["parts"][0] = (
                        f"{content}\n\n{genai_messages[0]['parts'][0]}"
                    )
                else:
                    # Add as user message if no user messages yet
                    genai_messages.append({"role": "user", "parts": [content]})
            elif role == "user":
                genai_messages.append({"role": "user", "parts": [content]})
            elif role == "assistant":
                genai_messages.append({"role": "model", "parts": [content]})
            elif role == "tool":
                # Handle tool responses
                tool_result = {
                    "functionResponse": {
                        "name": msg.get("name", ""),
                        "response": {"result": content},
                    }
                }
                # Find the last message and append tool result
                if genai_messages and genai_messages[-1]["role"] == "model":
                    if "parts" not in genai_messages[-1]:
                        genai_messages[-1]["parts"] = []
                    genai_messages[-1]["parts"].append(tool_result)
                else:
                    # Create a new model message for tool result
                    genai_messages.append({"role": "model", "parts": [tool_result]})

        return genai_messages

    def _convert_tools_to_genai_format(self, tools: list[dict[str, Any]]) -> list[Any]:
        """Convert OpenAI-style tools to Gemini format."""
        genai_functions = []

        for tool in tools:
            if tool.get("type") == "function":
                func_def = tool["function"]

                # Convert parameters schema
                parameters = func_def.get("parameters", {})
                properties = parameters.get("properties", {})
                required = parameters.get("required", [])

                # Build Gemini function declaration
                genai_func = genai.protos.FunctionDeclaration(
                    name=func_def["name"],
                    description=func_def.get("description", ""),
                    parameters=genai.protos.Schema(
                        type=genai.protos.Type.OBJECT,
                        properties={
                            name: self._convert_property_schema(prop)
                            for name, prop in properties.items()
                        },
                        required=required,
                    ),
                )
                genai_functions.append(genai_func)

        return (
            [genai.Tool(function_declarations=genai_functions)]
            if genai_functions
            else []
        )

    def _convert_property_schema(self, prop: dict[str, Any]) -> Any:
        """Convert a single property schema to Gemini format."""
        prop_type = prop.get("type", "string")

        # Map types
        type_mapping = {
            "string": genai.protos.Type.STRING,
            "number": genai.protos.Type.NUMBER,
            "integer": genai.protos.Type.INTEGER,
            "boolean": genai.protos.Type.BOOLEAN,
            "array": genai.protos.Type.ARRAY,
            "object": genai.protos.Type.OBJECT,
        }

        schema_dict = {
            "type": type_mapping.get(prop_type, genai.protos.Type.STRING),
            "description": prop.get("description", ""),
        }

        # Handle arrays
        if prop_type == "array" and "items" in prop:
            schema_dict["items"] = self._convert_property_schema(prop["items"])

        # Handle enums
        if "enum" in prop:
            schema_dict["enum"] = prop["enum"]

        return genai.protos.Schema(**schema_dict)

    async def generate_response(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Generate response using Google GenAI."""
        try:
            # Convert messages to Gemini format
            genai_messages = self._convert_messages_to_genai_format(messages)

            # Build generation config
            generation_config = {
                **self.default_kwargs,
                **self._get_model_specific_params(self.model_name),
            }

            # Handle tools
            genai_tools = None
            if tools:
                genai_tools = self._convert_tools_to_genai_format(tools)
                # Note: Gemini doesn't have direct equivalent of tool_choice
                # It always allows the model to decide whether to use tools

            # Make API call
            response = await self.model.generate_content_async(
                contents=genai_messages,
                tools=genai_tools,
                generation_config=generation_config,
            )

            # Parse response
            content = None
            tool_calls = None

            if response.candidates and response.candidates[0].content.parts:
                parts = response.candidates[0].content.parts

                # Extract text content
                text_parts = [
                    part.text for part in parts if hasattr(part, "text") and part.text
                ]
                if text_parts:
                    content = "".join(text_parts)

                # Extract function calls
                function_calls = [
                    part for part in parts if hasattr(part, "function_call")
                ]
                if function_calls:
                    tool_calls = []
                    for i, fc in enumerate(function_calls):
                        # Generate ID for function call (Gemini doesn't provide one)
                        call_id = f"call_{i}_{fc.function_call.name}"

                        # Convert arguments
                        args = {}
                        if hasattr(fc.function_call, "args"):
                            # Convert protobuf Struct to dict
                            for key, value in fc.function_call.args.items():
                                args[key] = self._convert_protobuf_value(value)

                        tool_calls.append(
                            ToolCallItem(
                                id=call_id,
                                type="function",
                                function=ToolCallFunction(
                                    name=fc.function_call.name,
                                    arguments=json.dumps(args),
                                ),
                            )
                        )

            # Extract usage information
            reasoning_info = None
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                reasoning_info = {
                    "prompt_tokens": response.usage_metadata.prompt_token_count,
                    "completion_tokens": response.usage_metadata.candidates_token_count,
                    "total_tokens": response.usage_metadata.total_token_count,
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

    def _convert_protobuf_value(self, value: Any) -> Any:
        """Convert protobuf Value to Python object."""
        if hasattr(value, "string_value"):
            return value.string_value
        elif hasattr(value, "number_value"):
            return value.number_value
        elif hasattr(value, "bool_value"):
            return value.bool_value
        elif hasattr(value, "struct_value"):
            # Convert struct to dict
            return {
                k: self._convert_protobuf_value(v)
                for k, v in value.struct_value.items()
            }
        elif hasattr(value, "list_value"):
            # Convert list
            return [self._convert_protobuf_value(v) for v in value.list_value]
        else:
            # Fallback - try to extract value directly
            return str(value)

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
    ) -> dict[str, Any]:
        """
        Format user message with optional file content.

        Gemini supports native file uploads for images and other content.
        """
        if not file_path:
            # No file, just text
            return {"role": "user", "content": prompt_text or ""}

        # For Gemini, we can use the Files API for better handling
        # For now, we'll use the simpler approach of including files inline

        if mime_type and mime_type.startswith("image/"):
            # Upload image file
            uploaded_file = genai.upload_file(file_path, mime_type=mime_type)
            logger.info(f"Uploaded file to Gemini: {uploaded_file.uri}")

            # Create message with file reference
            parts = []
            if prompt_text:
                parts.append(prompt_text)
            parts.append(uploaded_file)

            return {"role": "user", "content": parts}

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
