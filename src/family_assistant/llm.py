"""
Module defining the interface and implementations for interacting with Large Language Models (LLMs).
"""

import copy  # For deep copying tool definitions
import json
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import aiofiles  # For async file operations
import litellm  # Import litellm
from litellm import acompletion
from litellm.exceptions import (
    APIConnectionError,
    APIError,
    BadRequestError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)

# Removed ChatCompletionToolParam as it's causing ImportError and not explicitly used

if TYPE_CHECKING:
    from litellm.types.completion import ChatCompletionMessageParam
    from litellm.types.files import (
        FileResponse,  # Corrected import path and moved to TYPE_CHECKING
    )

logger = logging.getLogger(__name__)


# --- Conditionally Enable LiteLLM Debug Logging ---
LITELLM_DEBUG_ENABLED = os.getenv("LITELLM_DEBUG", "false").lower() in (
    "true",
    "1",
    "yes",
)
if LITELLM_DEBUG_ENABLED:
    litellm.set_verbose = True
    logger.info(
        "Enabled LiteLLM verbose logging (set_verbose=True) because LITELLM_DEBUG is set."
    )
else:
    logger.info("LiteLLM verbose logging is disabled (LITELLM_DEBUG not set or false).")
# --- End Debug Logging Control ---


@dataclass
class LLMOutput:
    """Standardized output structure from an LLM call."""

    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = field(
        default=None
    )  # Store raw tool call dicts
    reasoning_info: dict[str, Any] | None = field(
        default=None
    )  # Store reasoning/usage data


def _sanitize_tools_for_litellm(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Removes unsupported 'format' fields from string parameters in tool definitions
    before sending them to LiteLLM/OpenAI, which only supports 'enum' and 'date-time'.

    Args:
        tools: A list of tool definitions in OpenAI dictionary format.

    Returns:
        A new list of sanitized tool definitions.
    """
    # Create a deep copy to avoid modifying the original list in place
    sanitized_tools = copy.deepcopy(tools)

    for tool_dict in sanitized_tools:
        func_def = tool_dict.get("function", {})
        params = func_def.get("parameters", {})
        properties = params.get("properties", {})
        tool_name = func_def.get("name", "unknown_tool")  # For logging context

        if not isinstance(properties, dict):
            logger.warning(
                f"Sanitizing tool '{tool_name}': Non-dict 'properties' found. Skipping property sanitization for this tool."
            )
            continue

        props_to_delete_format = []
        for param_name, param_details in properties.items():
            if isinstance(param_details, dict):
                param_type = param_details.get("type")
                param_format = param_details.get("format")

                if (
                    param_type == "string"
                    and param_format
                    and param_format not in ["enum", "date-time"]
                ):
                    logger.warning(
                        f"Sanitizing tool '{tool_name}': Removing unsupported format '{param_format}' from string parameter '{param_name}' for LiteLLM compatibility."
                    )
                    props_to_delete_format.append(param_name)

        for param_name in props_to_delete_format:
            if (
                param_name in properties
                and isinstance(properties[param_name], dict)
                and "format" in properties[param_name]
            ):
                del properties[param_name]["format"]

    return sanitized_tools


class LLMInterface(Protocol):
    """Protocol defining the interface for interacting with an LLM."""

    async def generate_response(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """
        Generates a response from the LLM based on a pre-structured list of messages.
        (Existing method for direct message-based interaction)
        """
        ...

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
    ) -> dict[str, Any]:
        """
        Formats a user message, potentially including file content.
        The client decides how to represent the file (e.g., Gemini Files API ref, base64 data URI).

        Args:
            prompt_text: The user's textual prompt. Can be None if the primary input is a file.
            file_path: Path to the file to be processed. Can be None.
            mime_type: MIME type of the file. Required if file_path is provided.
            max_text_length: Optional maximum length for text content truncation (applies if no file or text file).

        Returns:
            A dictionary representing a single user message, e.g.,
            {"role": "user", "content": "..."} or {"role": "user", "content": [...]}.
        """
        ...


class LiteLLMClient:
    """LLM client implementation using the LiteLLM library."""

    def __init__(
        self,
        model: str,
        model_parameters: dict[str, Any] | None = None,
        **kwargs: dict[str, Any],
    ) -> None:
        """
        Initializes the LiteLLM client.

        Args:
            model: The identifier of the model to use (e.g., "openrouter/google/gemini-flash-1.5").
            model_parameters: A dictionary where keys are model names/prefixes
                              and values are dicts of parameters specific to those models.
            **kwargs: Additional keyword arguments to pass directly to litellm.acompletion
                      on every call (e.g., temperature, max_tokens). These are
                      applied *before* model-specific parameters.
        """
        if not model:
            raise ValueError("LLM model identifier cannot be empty.")
        self.model = model
        self.default_kwargs = kwargs  # Store base kwargs
        self.model_parameters = model_parameters or {}  # Store model-specific params
        logger.info(
            f"LiteLLMClient initialized for model: {self.model} "
            f"with default kwargs: {self.default_kwargs} "
            f"and model-specific parameters: {self.model_parameters}"
        )

    async def generate_response(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Generates a response using LiteLLM from a pre-structured message list."""
        model_arg: str = self.model
        messages_arg: list[dict[str, Any]] = messages

        # Start with default kwargs passed during initialization (e.g., temperature, max_tokens)
        completion_params: dict[str, Any] = self.default_kwargs.copy()

        # Find and merge model-specific parameters from config
        reasoning_params_config = None
        for pattern, params in self.model_parameters.items():
            matched = False
            if pattern.endswith("-"):  # Prefix match
                if self.model.startswith(pattern[:-1]):
                    matched = True
            elif self.model == pattern:  # Exact match
                matched = True

            if matched:
                logger.debug(f"Applying parameters for pattern '{pattern}': {params}")
                params_to_merge = params.copy()
                if "reasoning" in params_to_merge and isinstance(
                    params_to_merge["reasoning"], dict
                ):
                    reasoning_params_config = params_to_merge.pop("reasoning")
                completion_params.update(params_to_merge)
                break  # Stop after first matching pattern

        # Add the 'reasoning' object to completion_params for OpenRouter models if configured
        if self.model.startswith("openrouter/") and reasoning_params_config:
            completion_params["reasoning"] = reasoning_params_config
            logger.debug(
                f"Adding 'reasoning' parameter for OpenRouter: {reasoning_params_config}"
            )

        try:
            if tools:
                sanitized_tools_arg = _sanitize_tools_for_litellm(tools)
                logger.debug(
                    f"Calling LiteLLM model {model_arg} with {len(messages_arg)} messages. "
                    f"Tools provided. Tool choice: {tool_choice}. Other params: {json.dumps(completion_params, default=str)}"
                )
                response = await acompletion(
                    model=model_arg,
                    messages=messages_arg,
                    tools=sanitized_tools_arg,
                    tool_choice=tool_choice,
                    **completion_params,
                )
            else:
                logger.debug(
                    f"Calling LiteLLM model {model_arg} with {len(messages_arg)} messages. "
                    f"No tools provided. Other params: {json.dumps(completion_params, default=str)}"
                )
                response = await acompletion(
                    model=model_arg,
                    messages=messages_arg,
                    **completion_params,
                )

            # Extract response message
            response_message: ChatCompletionMessageParam | None = (
                response.choices[0].message if response.choices else None
            )

            if not response_message:
                logger.warning(
                    f"LiteLLM response structure unexpected or empty: {response}"
                )
                raise APIError(
                    message="Received empty or unexpected response from LiteLLM.",
                    llm_provider="litellm",
                    model=self.model,
                    status_code=500,
                )

            # Extract content, tool calls, and potentially reasoning/usage info
            content = response_message.get("content")
            raw_tool_calls = response_message.get("tool_calls")
            reasoning_info = None

            if hasattr(response, "usage") and response.usage:
                try:
                    reasoning_info = response.usage.model_dump(mode="json")
                    logger.debug(
                        f"Extracted usage data as reasoning_info: {reasoning_info}"
                    )
                except Exception as usage_err:
                    logger.warning(f"Could not serialize response.usage: {usage_err}")

            tool_calls_list = []
            if raw_tool_calls:
                for tc in raw_tool_calls:
                    # tc should be a dict-like structure (ToolCall)
                    tool_calls_list.append(
                        dict(tc)
                    )  # Ensure it's a plain dict for LLMOutput

            logger.debug(
                f"LiteLLM response received. Content: {bool(content)}. Tool Calls: {len(tool_calls_list)}. Reasoning: {bool(reasoning_info)}"
            )
            return LLMOutput(
                content=content,  # type: ignore # content can be str | None
                tool_calls=tool_calls_list if tool_calls_list else None,
                reasoning_info=reasoning_info,
            )
        except (
            APIConnectionError,
            Timeout,
            RateLimitError,
            ServiceUnavailableError,
            APIError,
            BadRequestError,
        ) as e:
            logger.error(
                f"LiteLLM API error for model {self.model}: {e}", exc_info=True
            )
            logger.info("Input passed to model: %r", messages)
            raise  # Re-raise the specific LiteLLM exception
        except Exception as e:
            logger.error(
                f"Unexpected error during LiteLLM call for model {self.model}: {e}",
                exc_info=True,
            )
            # Wrap unexpected errors in a generic APIError or a custom exception
            raise APIError(
                message=f"Unexpected error: {e}",
                llm_provider="litellm",
                model=self.model,
                status_code=500,
            ) from e

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
    ) -> dict[str, Any]:
        import asyncio  # For running sync litellm.create_file in thread
        import base64  # Import here as it's only used in this method

        user_content_parts: list[dict[str, Any]] = []
        actual_prompt_text = prompt_text or "Process the provided file."

        if file_path and mime_type:
            # Attempt Gemini File API if applicable
            if self.model.startswith("gemini/"):
                try:
                    logger.info(
                        f"Attempting to upload file to Gemini: {file_path} ({mime_type})"
                    )
                    if not os.getenv("GEMINI_API_KEY"):
                        raise ValueError(
                            "GEMINI_API_KEY not found in environment for Gemini file upload."
                        )

                    async with aiofiles.open(file_path, "rb") as f_bytes_io:
                        file_bytes_content = await f_bytes_io.read()

                    loop = asyncio.get_running_loop()
                    # Use litellm.file_upload for more generic provider support
                    gemini_api_key = os.getenv("GEMINI_API_KEY")
                    if not gemini_api_key:  # Redundant check, but good practice
                        raise ValueError("GEMINI_API_KEY is required.")

                    gemini_file_obj: FileResponse = await loop.run_in_executor(
                        None,  # Default ThreadPoolExecutor
                        litellm.file_upload,
                        file_bytes_content,  # file (bytes)
                        os.path.basename(file_path),  # file_name
                        "gemini",  # custom_llm_provider
                        gemini_api_key,  # api_key
                        # model argument is optional for file_upload, let gemini provider handle
                    )
                    logger.info(f"File uploaded to Gemini, ID: {gemini_file_obj.id}")
                    user_content_parts.append({
                        "type": "text",
                        "text": actual_prompt_text,
                    })
                    user_content_parts.append({
                        "type": "file",
                        "file": {
                            "file_id": gemini_file_obj.id,
                            "filename": os.path.basename(
                                file_path
                            ),  # Consistent filename
                            "format": mime_type,  # Use provided mime_type
                        },
                    })
                except Exception as e:
                    logger.error(
                        f"Failed to upload file to Gemini or construct message: {e}. Falling back to base64/text.",
                        exc_info=True,
                    )
                    user_content_parts = []  # Ensure fallback if Gemini fails

            # Fallback or non-Gemini model file handling
            if (
                not user_content_parts
            ):  # Only if Gemini part didn't populate or wasn't attempted
                if mime_type.startswith("image/"):
                    try:
                        async with aiofiles.open(file_path, "rb") as f:
                            image_bytes = await f.read()
                        encoded_image = base64.b64encode(image_bytes).decode("utf-8")
                        image_url = f"data:{mime_type};base64,{encoded_image}"
                        user_content_parts.append({
                            "type": "text",
                            "text": actual_prompt_text,
                        })
                        user_content_parts.append({
                            "type": "image_url",
                            "image_url": {"url": image_url},
                        })
                    except Exception as e:
                        logger.error(
                            f"Failed to read/encode image {file_path}: {e}",
                            exc_info=True,
                        )
                        user_content_parts.append({
                            "type": "text",
                            "text": actual_prompt_text,
                        })  # Fallback to text
                elif mime_type.startswith("text/"):
                    try:
                        async with (
                            aiofiles.open(file_path, encoding="utf-8") as f
                        ):  # Changed from "r" to "rb" for consistency, but text files should be "r"
                            file_text_content = await f.read()
                        combined_text = f"{actual_prompt_text}\n\n--- File Content ---\n{file_text_content}"
                        if max_text_length and len(combined_text) > max_text_length:
                            logger.info(
                                f"Truncating combined text from {len(combined_text)} to {max_text_length} chars."
                            )
                            combined_text = combined_text[:max_text_length]
                        user_content_parts.append({
                            "type": "text",
                            "text": combined_text,
                        })
                    except Exception as e:
                        logger.error(
                            f"Failed to read text file {file_path}: {e}", exc_info=True
                        )
                        user_content_parts.append({
                            "type": "text",
                            "text": actual_prompt_text,
                        })  # Fallback to text
                else:  # Other file types
                    logger.warning(
                        f"File type {mime_type} for {file_path} not specifically handled for image/text. "
                        "Attempting generic base64 data URI."
                    )
                    try:
                        async with aiofiles.open(file_path, "rb") as f_bytes_io:
                            file_bytes = await f_bytes_io.read()
                        encoded_file_data = base64.b64encode(file_bytes).decode("utf-8")
                        file_data_uri = f"data:{mime_type};base64,{encoded_file_data}"
                        user_content_parts.append({
                            "type": "text",
                            "text": actual_prompt_text,
                        })
                        user_content_parts.append({
                            "type": "file",
                            "file": {"file_data": file_data_uri},
                        })
                        logger.info(
                            f"Prepared generic file {file_path} as base64 data URI for LLM."
                        )
                    except Exception as e:
                        logger.error(
                            f"Failed to read/encode generic file {file_path} as base64: {e}",
                            exc_info=True,
                        )
                        user_content_parts.append({
                            "type": "text",
                            "text": actual_prompt_text,
                        })  # Fallback to text

        elif prompt_text:  # Only text prompt provided
            text_to_send = prompt_text
            if max_text_length and len(text_to_send) > max_text_length:
                logger.info(
                    f"Truncating prompt text from {len(text_to_send)} to {max_text_length} chars."
                )
                text_to_send = text_to_send[:max_text_length]
            user_content_parts.append({"type": "text", "text": text_to_send})
        else:
            logger.error(
                "format_user_message_with_file called with no file and no prompt text."
            )
            raise ValueError("Cannot format user message with no input (file or text).")

        # Determine final content structure for the user message
        final_user_content: str | list[dict[str, Any]]
        if len(user_content_parts) == 1 and user_content_parts[0]["type"] == "text":
            final_user_content = user_content_parts[0]["text"]
        else:
            final_user_content = user_content_parts

        return {"role": "user", "content": final_user_content}


class RecordingLLMClient:
    """
    An LLM client wrapper that records interactions (inputs and outputs)
    to a file while proxying calls to another LLM client.
    """

    def __init__(self, wrapped_client: LLMInterface, recording_path: str) -> None:
        """
        Initializes the recording client.

        Args:
            wrapped_client: The actual LLMInterface instance to use for generation.
            recording_path: Path to the file where interactions will be recorded (JSON Lines format).
        """
        if not hasattr(wrapped_client, "generate_response"):
            raise TypeError("wrapped_client must implement the LLMInterface protocol.")
        self.wrapped_client = wrapped_client
        self.recording_path = recording_path
        # Ensure directory exists (optional, depends on desired behavior)
        os.makedirs(os.path.dirname(self.recording_path), exist_ok=True)
        logger.info(
            f"RecordingLLMClient initialized. Wrapping {type(wrapped_client).__name__}. Recording to: {self.recording_path}"
        )

    async def generate_response(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Calls the wrapped client's standard generate_response, records, and returns."""
        # This method is for the existing generate_response interface
        input_data = {
            "method": "generate_response",
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
        }
        try:
            output_data = await self.wrapped_client.generate_response(
                messages=messages, tools=tools, tool_choice=tool_choice
            )
            await self._record_interaction(input_data, output_data)
            return output_data
        except Exception as e:
            logger.error(
                f"Error in RecordingLLMClient.generate_response: {e}", exc_info=True
            )
            # Optionally record the error state as well, or just re-raise
            # For now, just re-raise to ensure error propagation.
            raise

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
    ) -> dict[str, Any]:
        """Calls the wrapped client's format_user_message_with_file, records, and returns."""
        input_data = {
            "method": "format_user_message_with_file",
            "prompt_text": prompt_text,
            "file_path": file_path,
            "mime_type": mime_type,
            "max_text_length": max_text_length,
        }
        try:
            # Note: output_data for this method is a dict, not LLMOutput
            output_dict = await self.wrapped_client.format_user_message_with_file(
                prompt_text=prompt_text,
                file_path=file_path,
                mime_type=mime_type,
                max_text_length=max_text_length,
            )
            # For recording, we'll adapt the _record_interaction or create a new one
            # For simplicity, let's assume _record_interaction can handle a dict as "output"
            # or we make a small adjustment. Let's record it as a simple dict.
            record = {"input": input_data, "output": output_dict}
            await self._write_record_to_file(record)
            return output_dict
        except Exception as e:
            logger.error(
                f"Error in RecordingLLMClient.format_user_message_with_file: {e}",
                exc_info=True,
            )
            raise

    async def _record_interaction(
        self,
        input_data: dict[str, Any],
        output_data: LLMOutput,  # This is for generate_response
    ) -> None:
        # Ensure output_data is serializable (LLMOutput should be)
        record = {"input": input_data, "output": output_data.__dict__}
        await self._write_record_to_file(record)

    async def _write_record_to_file(self, record: dict[str, Any]) -> None:
        """Helper method to write a generic record to the recording file."""
        try:
            async with aiofiles.open(
                self.recording_path, mode="a", encoding="utf-8"
            ) as f:
                await f.write(
                    json.dumps(record, ensure_ascii=False, default=str) + "\n"
                )  # Added default=str
            logger.debug(f"Recorded interaction to {self.recording_path}")
        except Exception as file_err:
            logger.error(
                f"Failed to write interaction to recording file {self.recording_path}: {file_err}",
                exc_info=True,
            )


class PlaybackLLMClient:
    """
    An LLM client that plays back previously recorded interactions from a file.
    Plays back recorded interactions by matching the input arguments.
    """

    def __init__(self, recording_path: str) -> None:
        """
        Initializes the playback client by loading all recorded interactions.

        Args:
            recording_path: Path to the JSON Lines file containing recorded interactions.

        Raises:
            FileNotFoundError: If the recording file does not exist.
            ValueError: If the recording file is empty or contains invalid JSON.
        """
        self.recording_path = recording_path
        self.recorded_interactions: list[dict[str, Any]] = []
        logger.info(
            f"PlaybackLLMClient initializing. Reading from: {self.recording_path}"
        )
        try:
            # Load all interactions into memory synchronously during init
            # For async loading, this would need to be an async factory or method
            with open(self.recording_path, encoding="utf-8") as f:
                line_num = 0
                for line in f:
                    line_num += 1
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        if "input" not in record or "output" not in record:
                            logger.warning(
                                f"Skipping line {line_num} in {self.recording_path}: Missing 'input' or 'output' key."
                            )
                            continue
                        self.recorded_interactions.append(record)
                    except json.JSONDecodeError:
                        logger.warning(
                            f"Skipping invalid JSON on line {line_num} in {self.recording_path}: {line[:100]}..."
                        )
                    except Exception as parse_err:
                        logger.warning(
                            f"Error parsing record on line {line_num} in {self.recording_path}: {parse_err}"
                        )

            if not self.recorded_interactions:
                logger.warning(
                    f"Recording file {self.recording_path} is empty or contains no valid records."
                )
                # Decide whether to raise an error or allow initialization with empty list
                # Raising error is safer to prevent unexpected behavior later.
                raise ValueError(
                    f"No valid interactions loaded from {self.recording_path}"
                )

            logger.info(
                f"PlaybackLLMClient initialized. Loaded {len(self.recorded_interactions)} interactions from: {self.recording_path}"
            )

        except FileNotFoundError:
            logger.error(f"Recording file not found: {self.recording_path}")
            raise  # Re-raise FileNotFoundError
        except Exception as e:
            logger.error(
                f"Failed to read or parse recording file {self.recording_path}: {e}",
                exc_info=True,
            )
            # Wrap other errors in a ValueError for consistent init failure reporting
            raise ValueError(
                f"Failed to load recording file {self.recording_path}: {e}"
            ) from e

    async def generate_response(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Plays back for the standard generate_response method."""
        current_input_args = {
            "method": "generate_response",
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
        }
        return await self._find_and_playback_llm_output(current_input_args)

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
    ) -> dict[str, Any]:
        """Plays back for the format_user_message_with_file method."""
        current_input_args = {
            "method": "format_user_message_with_file",
            "prompt_text": prompt_text,
            "file_path": file_path,
            "mime_type": mime_type,
            "max_text_length": max_text_length,
        }
        # This method returns a dict, not LLMOutput
        return await self._find_and_playback_dict(current_input_args)

    async def _find_and_playback_llm_output(
        self, current_input_args: dict[str, Any]
    ) -> LLMOutput:
        """Helper to find and playback interactions that return LLMOutput."""
        logger.debug(
            f"PlaybackLLMClient attempting to find LLMOutput match for input args: {json.dumps(current_input_args, indent=2, default=str)[:500]}..."
        )
        for record in self.recorded_interactions:
            if record.get("input") == current_input_args:
                logger.info(f"Found matching interaction in {self.recording_path}.")
                output_data = record["output"]
                if not isinstance(output_data, dict) or not all(
                    k in output_data for k in ["content", "tool_calls"]
                ):  # Basic check for LLMOutput structure
                    logger.error(
                        f"Recorded output for matched input is not a valid LLMOutput structure: {output_data}"
                    )
                    raise LookupError(
                        "Matched recorded output is not a valid LLMOutput structure."
                    )

                matched_output = LLMOutput(
                    content=output_data.get("content"),
                    tool_calls=output_data.get("tool_calls"),
                    reasoning_info=output_data.get("reasoning_info"),
                )
                logger.debug(
                    f"Playing back matched LLMOutput. Content: {bool(matched_output.content)}. Tool Calls: {len(matched_output.tool_calls) if matched_output.tool_calls else 0}"
                )
                return matched_output

        await self._log_no_match_error(current_input_args)
        raise LookupError(
            f"No matching LLMOutput recorded interaction found in {self.recording_path} for the current input args."
        )

    async def _find_and_playback_dict(
        self, current_input_args: dict[str, Any]
    ) -> dict[str, Any]:
        """Helper to find and playback interactions that return a simple dict."""
        logger.debug(
            f"PlaybackLLMClient attempting to find dict match for input args: {json.dumps(current_input_args, indent=2, default=str)[:500]}..."
        )
        for record in self.recorded_interactions:
            if record.get("input") == current_input_args:
                logger.info(f"Found matching interaction in {self.recording_path}.")
                output_data = record["output"]
                if not isinstance(output_data, dict):
                    logger.error(
                        f"Recorded output for matched input is not a dict: {output_data}"
                    )
                    raise LookupError("Matched recorded output is not a dictionary.")
                logger.debug(f"Playing back matched dict: {output_data}")
                return output_data

        await self._log_no_match_error(current_input_args)
        raise LookupError(
            f"No matching dict recorded interaction found in {self.recording_path} for the current input args."
        )

    async def _log_no_match_error(self, current_input_args: dict[str, Any]) -> None:
        """Logs an error when no matching interaction is found."""
        logger.error(
            f"PlaybackLLMClient: No matching interaction found in {self.recording_path} for the provided input args."
        )
        try:
            failed_input_str = json.dumps(current_input_args, indent=2, default=str)
        except Exception:
            failed_input_str = str(current_input_args)
        logger.error(f"Failed Input Args:\n{failed_input_str}")
