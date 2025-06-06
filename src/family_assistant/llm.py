"""
Module defining the interface and implementations for interacting with Large Language Models (LLMs).
"""

import copy  # For deep copying tool definitions
import io
import json
import logging
import os
from dataclasses import asdict, dataclass, field  # Added asdict
from typing import TYPE_CHECKING, Any, Protocol, cast

import aiofiles  # type: ignore[import-untyped] # For async file operations
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
from litellm.utils import get_valid_params  # Import for filtering kwargs

# Removed ChatCompletionToolParam as it's causing ImportError and not explicitly used

if TYPE_CHECKING:
    from litellm import Message  # Add import for litellm.Message
    from litellm.types.files import (
        FileResponse,  # type: ignore[attr-defined] # Changed import path
    )
    from litellm.types.utils import (
        ModelResponse,  # Import ModelResponse for type hinting
    )

logger = logging.getLogger(__name__)


# --- Conditionally Enable LiteLLM Debug Logging ---
LITELLM_DEBUG_ENABLED = os.getenv("LITELLM_DEBUG", "false").lower() in (
    "true",
    "1",
    "yes",
)
if LITELLM_DEBUG_ENABLED:
    litellm.set_verbose = True  # type: ignore[reportPrivateImportUsage]
    logger.info(
        "Enabled LiteLLM verbose logging (set_verbose = True) because LITELLM_DEBUG is set."
    )
else:
    logger.info("LiteLLM verbose logging is disabled (LITELLM_DEBUG not set or false).")
# --- End Debug Logging Control ---


@dataclass(frozen=True)
class ToolCallFunction:
    """Represents the function to be called in a tool call."""

    name: str
    arguments: str  # JSON string of arguments


@dataclass(frozen=True)
class ToolCallItem:
    """Represents a single tool call requested by the LLM."""

    id: str
    type: str  # Usually "function"
    function: ToolCallFunction


@dataclass
class LLMOutput:
    """Standardized output structure from an LLM call."""

    content: str | None = None
    tool_calls: list[ToolCallItem] | None = field(default=None)
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
        model_parameters: dict[str, dict[str, Any]] | None = None,  # Corrected type
        fallback_model_id: str | None = None,
        fallback_model_parameters: dict[
            str, dict[str, Any]
        ] | None = None,  # Corrected type
        **kwargs: dict[str, Any],
    ) -> None:
        """
        Initializes the LiteLLM client.

        Args:
            model: The identifier of the primary model to use.
            model_parameters: Parameters specific to the primary model (pattern -> params_dict).
            fallback_model_id: Optional identifier for a fallback model.
            fallback_model_parameters: Optional parameters for the fallback model (pattern -> params_dict).
            **kwargs: Default keyword arguments for litellm.acompletion.
        """
        if not model:
            raise ValueError("LLM model identifier cannot be empty.")
        self.model = model
        self.default_kwargs = kwargs
        self.model_parameters: dict[
            str, dict[str, Any]
        ] = model_parameters or {}  # Ensure correct type for self
        self.fallback_model_id = fallback_model_id
        self.fallback_model_parameters: dict[
            str, dict[str, Any]
        ] = fallback_model_parameters or {}  # Ensure correct type for self
        logger.info(
            f"LiteLLMClient initialized for primary model: {self.model} "
            f"with default kwargs: {self.default_kwargs}, "
            f"model-specific parameters: {self.model_parameters}. "
            f"Fallback model: {self.fallback_model_id}, "
            f"fallback params: {self.fallback_model_parameters}"
        )

    async def _attempt_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: str | None,
        specific_model_params: dict[str, dict[str, Any]],  # Corrected type
    ) -> LLMOutput:
        """Internal method to make a single attempt at LLM completion."""
        completion_params = self.default_kwargs.copy()

        # Find and merge model-specific parameters from config for the current model_id
        reasoning_params_config = None
        # specific_model_params is the dict of (pattern -> params_dict) for the current model type
        current_model_config_params = specific_model_params

        for pattern, params in current_model_config_params.items():  # params is dict[str, Any]
            matched = False
            if pattern.endswith("-"):
                if model_id.startswith(pattern[:-1]):
                    matched = True
            elif model_id == pattern:
                matched = True

            if matched:
                logger.debug(
                    f"Applying parameters for model '{model_id}' using pattern '{pattern}': {params}"
                )
                params_to_merge = params.copy()
                if "reasoning" in params_to_merge and isinstance(
                    params_to_merge["reasoning"], dict
                ):
                    reasoning_params_config = params_to_merge.pop("reasoning")
                completion_params.update(params_to_merge)
                break

        if model_id.startswith("openrouter/") and reasoning_params_config:
            completion_params["reasoning"] = reasoning_params_config
            logger.debug(
                f"Adding 'reasoning' parameter for OpenRouter model '{model_id}': {reasoning_params_config}"
            )

        # Filter completion_params to include only valid litellm.acompletion arguments
        # get_valid_params() returns a list of valid kwargs for litellm.completion/acompletion
        valid_acompletion_params_set = set(get_valid_params())
        final_completion_params = {
            k: v
            for k, v in completion_params.items()
            if k in valid_acompletion_params_set
        }

        discarded_keys = set(completion_params.keys()) - set(
            final_completion_params.keys()
        )
        if discarded_keys:
            logger.warning(
                f"For model {model_id}, discarded the following non-standard acompletion parameters: {discarded_keys}. "
                f"Original params included: {list(completion_params.keys())}"
            )

        if tools:
            sanitized_tools_arg = _sanitize_tools_for_litellm(tools)
            logger.debug(
                f"Calling LiteLLM model {model_id} with {len(messages)} messages. "
                f"Tools provided. Tool choice: {tool_choice}. Filtered params: {json.dumps(final_completion_params, default=str)}"
            )
            response = await acompletion(
                model=model_id,
                messages=messages,
                tools=sanitized_tools_arg,
                tool_choice=tool_choice,
                stream=False,
                **final_completion_params,
            )
            response = cast("ModelResponse", response)
        else:
            logger.debug(
                f"Calling LiteLLM model {model_id} with {len(messages)} messages. "
                f"No tools provided. Filtered params: {json.dumps(final_completion_params, default=str)}"
            )
            _response_obj = await acompletion(
                model=model_id,
                messages=messages,
                stream=False,
                **final_completion_params,
            )
            response = cast("ModelResponse", _response_obj)

        response_message: Message | None = None
        if response.choices:
            response_message = response.choices[0].message  # type: ignore[attr-defined]

        if not response_message:
            logger.warning(
                f"LiteLLM response structure unexpected or empty for model {model_id}: {response}"
            )
            raise APIError(
                message="Received empty or unexpected response from LiteLLM.",
                llm_provider="litellm",
                model=model_id,
                status_code=500,
            )

        content = response_message.get("content")
        raw_tool_calls = response_message.get("tool_calls")
        reasoning_info = None
        if hasattr(response, "usage") and response.usage:  # type: ignore[attr-defined]
            try:
                reasoning_info = response.usage.model_dump(mode="json")  # type: ignore[attr-defined]
            except Exception as usage_err:
                logger.warning(
                    f"Could not serialize response.usage for model {model_id}: {usage_err}"
                )  # type: ignore[attr-defined]

        tool_calls_list = []
        if raw_tool_calls:
            for tc_obj in raw_tool_calls:
                func_name: str | None = None
                func_args: str | None = None
                if hasattr(tc_obj, "function") and tc_obj.function:
                    if hasattr(tc_obj.function, "name"):
                        func_name = tc_obj.function.name
                    if hasattr(tc_obj.function, "arguments"):
                        func_args = tc_obj.function.arguments
                else:
                    logger.warning(
                        f"ToolCall object for model {model_id} is missing function attribute or it's None: {tc_obj}"
                    )

                if not func_name or func_args is None:
                    logger.warning(
                        f"ToolCall's function object for model {model_id} is missing name or arguments: name='{func_name}', args_present={func_args is not None}."
                    )
                    tool_call_function = ToolCallFunction(
                        name=func_name or "malformed_function_in_llm_output",
                        arguments=func_args or "{}",
                    )
                else:
                    tool_call_function = ToolCallFunction(
                        name=func_name, arguments=func_args
                    )

                tc_id = tc_obj.id if hasattr(tc_obj, "id") else None
                tc_type = tc_obj.type if hasattr(tc_obj, "type") else None
                if not tc_id or not tc_type:
                    logger.error(
                        f"ToolCall item from LLM model {model_id} missing id ('{tc_id}') or type ('{tc_type}'). Skipping."
                    )
                    continue
                tool_calls_list.append(
                    ToolCallItem(id=tc_id, type=tc_type, function=tool_call_function)
                )

        logger.debug(
            f"LiteLLM response received from model {model_id}. Content: {bool(content)}. Tool Calls: {len(tool_calls_list)}. Reasoning: {bool(reasoning_info)}"
        )
        return LLMOutput(
            content=content,  # type: ignore
            tool_calls=tool_calls_list if tool_calls_list else None,
            reasoning_info=reasoning_info,
        )

    async def generate_response(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Generates a response using LiteLLM, with one retry on primary model and fallback."""
        retriable_errors = (
            APIConnectionError,
            Timeout,
            RateLimitError,
            ServiceUnavailableError,
        )
        last_exception: Exception | None = None

        # Attempt 1: Primary model
        try:
            logger.info(f"Attempt 1: Primary model ({self.model})")
            return await self._attempt_completion(
                model_id=self.model,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                specific_model_params=self.model_parameters,
            )
        except BadRequestError as e:
            logger.error(
                f"BadRequestError with primary model {self.model}. Not retrying or falling back: {e}"
            )
            raise  # Do not retry or fallback on BadRequestError
        except retriable_errors as e:
            logger.warning(
                f"Attempt 1 (Primary model {self.model}) failed with retriable error: {e}. Retrying primary model."
            )
            last_exception = e
        except APIError as e:  # Non-retriable APIError (but not BadRequestError)
            logger.warning(
                f"Attempt 1 (Primary model {self.model}) failed with APIError: {e}. Proceeding to fallback."
            )
            last_exception = e
        except Exception as e:
            logger.error(
                f"Attempt 1 (Primary model {self.model}) failed with unexpected error: {e}",
                exc_info=True,
            )
            last_exception = e  # Store for potential re-raise if fallback also fails or isn't attempted
            # For truly unexpected errors, we might still want to try fallback if configured.

        # Attempt 2: Retry Primary model (if Attempt 1 was a retriable error)
        if isinstance(last_exception, retriable_errors):
            try:
                logger.info(f"Attempt 2: Retrying primary model ({self.model})")
                return await self._attempt_completion(
                    model_id=self.model,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    specific_model_params=self.model_parameters,
                )
            except (
                BadRequestError
            ) as e:  # Should be rare if first attempt wasn't, but handle defensively
                logger.error(
                    f"BadRequestError on retry with primary model {self.model}. Not falling back: {e}"
                )
                raise
            except retriable_errors as e:
                logger.warning(
                    f"Attempt 2 (Retry Primary model {self.model}) failed with retriable error: {e}. Proceeding to fallback."
                )
                last_exception = e
            except APIError as e:  # Non-retriable APIError on retry
                logger.warning(
                    f"Attempt 2 (Retry Primary model {self.model}) failed with APIError: {e}. Proceeding to fallback."
                )
                last_exception = e
            except Exception as e:
                logger.error(
                    f"Attempt 2 (Retry Primary model {self.model}) failed with unexpected error: {e}",
                    exc_info=True,
                )
                last_exception = e

        # Attempt 3: Fallback model
        actual_fallback_model_id = self.fallback_model_id or "openai/o4-mini"
        if actual_fallback_model_id == self.model:
            logger.warning(
                f"Fallback model '{actual_fallback_model_id}' is the same as the primary model '{self.model}'. Skipping fallback."
            )
            if last_exception:
                raise last_exception
            # This case should ideally not happen if logic is correct, means no error but no success.
            raise APIError(
                message="All attempts failed without a specific error to raise.",
                llm_provider="litellm",
                model=self.model,
                status_code=500,
            )

        if last_exception:  # Ensure we only fallback if there was a prior failure
            logger.info(f"Attempt 3: Fallback model ({actual_fallback_model_id})")
            try:
                return await self._attempt_completion(
                    model_id=actual_fallback_model_id,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    specific_model_params=self.fallback_model_parameters,
                )
            except BadRequestError as e:
                logger.error(
                    f"BadRequestError with fallback model {actual_fallback_model_id}. Original error (if any) will be raised: {e}"
                )
                # Fallthrough to raise last_exception from primary model attempts
            except Exception as e:
                logger.error(
                    f"Attempt 3 (Fallback model {actual_fallback_model_id}) also failed: {e}",
                    exc_info=True,
                )
                # Fallthrough to raise last_exception from primary model attempts,
                # or this new one if last_exception was None (though it shouldn't be here).
                if not isinstance(
                    last_exception, BadRequestError
                ):  # Don't overwrite a BadRequest from primary with a fallback error
                    last_exception = e

        # If all attempts failed, raise the last significant exception
        if last_exception:
            logger.error(
                f"All LLM attempts failed. Raising last recorded exception: {last_exception}"
            )
            raise last_exception
        else:
            # Should not be reached if logic is correct, but as a safeguard:
            logger.error(
                "All LLM attempts failed without a specific exception captured."
            )
            raise APIError(
                message="All LLM attempts failed without a specific exception.",
                llm_provider="litellm",
                model=self.model,  # Or some generic indicator
                status_code=500,
            )

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
                        litellm.file_upload,  # type: ignore[attr-defined] # pylint: disable=no-member # Corrected path to file_upload
                        io.BytesIO(file_bytes_content),  # file (BinaryIO)
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
        # Convert ToolCallItem objects to dicts for JSON serialization
        output_dict = asdict(output_data)
        record = {"input": input_data, "output": output_dict}
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
                if not isinstance(output_data, dict):
                    logger.error(
                        f"Recorded output for matched input is not a dict: {output_data}"
                    )
                    raise LookupError("Matched recorded output is not a dictionary.")

                # Reconstruct ToolCallItem objects from dicts
                tool_calls_data = output_data.get("tool_calls")
                reconstructed_tool_calls: list[ToolCallItem] | None = None
                if isinstance(tool_calls_data, list):
                    reconstructed_tool_calls = []
                    for tc_dict in tool_calls_data:
                        if isinstance(tc_dict, dict):
                            func_dict = tc_dict.get("function")
                            if isinstance(func_dict, dict):
                                tool_call_function = ToolCallFunction(
                                    name=func_dict.get("name", "unknown_playback_func"),
                                    arguments=func_dict.get("arguments", "{}"),
                                )
                                reconstructed_tool_calls.append(
                                    ToolCallItem(
                                        id=tc_dict.get("id", "unknown_playback_id"),
                                        type=tc_dict.get("type", "function"),
                                        function=tool_call_function,
                                    )
                                )
                            else:
                                logger.warning(
                                    f"Skipping malformed function dict in playback: {func_dict}"
                                )
                        else:
                            logger.warning(
                                f"Skipping malformed tool_call item in playback: {tc_dict}"
                            )
                elif tool_calls_data is not None:
                    logger.warning(
                        f"Expected list for tool_calls in playback, got {type(tool_calls_data)}"
                    )

                matched_output = LLMOutput(
                    content=output_data.get("content"),
                    tool_calls=reconstructed_tool_calls,
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
