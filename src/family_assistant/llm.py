"""
Module defining the interface and implementations for interacting with Large Language Models (LLMs).
"""

import litellm # Import litellm
import json
import logging
import os
import aiofiles  # For async file operations
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Protocol, AsyncGenerator, AsyncIterator

from litellm import acompletion
from litellm.exceptions import (
    APIConnectionError,
    Timeout,
    RateLimitError,
    ServiceUnavailableError,
    APIError,
    BadRequestError,
)

# Removed ChatCompletionToolParam as it's causing ImportError and not explicitly used
from litellm.types.completion import ChatCompletionMessageParam

logger = logging.getLogger(__name__)


# --- Conditionally Enable LiteLLM Debug Logging ---
LITELLM_DEBUG_ENABLED = os.getenv("LITELLM_DEBUG", "false").lower() in ("true", "1", "yes")
if LITELLM_DEBUG_ENABLED:
    try:
        litellm._turn_on_debug() # Use the suggested internal function
        logger.info("Enabled LiteLLM internal debug logging via _turn_on_debug() because LITELLM_DEBUG is set.")
    except Exception as e:
        logger.error(f"Failed to enable LiteLLM debug logging: {e}", exc_info=True)
else:
    logger.info("LiteLLM internal debug logging is disabled (LITELLM_DEBUG not set or false).")
# --- End Debug Logging Control ---


@dataclass
class LLMOutput:
    """Standardized output structure from an LLM call."""

    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = field(default=None) # Store raw tool call dicts
    reasoning_info: Optional[Dict[str, Any]] = field(default=None) # Store reasoning/usage data


class LLMInterface(Protocol):
    """Protocol defining the interface for interacting with an LLM."""

    async def generate_response(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = "auto",
    ) -> LLMOutput:
        """
        Generates a response from the LLM based on the provided context.

        Args:
            messages: A list of message dictionaries representing the conversation history
                      and the current prompt. Expected format aligns with OpenAI/LiteLLM.
            tools: An optional list of tool definitions in OpenAI/LiteLLM format.
            tool_choice: Optional control over tool usage (e.g., "auto", "none", {"type": "function", "function": {"name": "my_function"}}).

        Returns:
            An LLMOutput object containing the response content and/or tool calls.

        Raises:
            Various exceptions (e.g., APIError, Timeout, ConnectionError) specific
            to the underlying LLM client implementation upon failure.
        """
        ...


class LiteLLMClient:
    """LLM client implementation using the LiteLLM library."""

    def __init__(
        self,
        model: str,
        model_parameters: Optional[Dict[str, Any]] = None,
        **kwargs: Any
    ):
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
        self.default_kwargs = kwargs # Store base kwargs
        self.model_parameters = model_parameters or {} # Store model-specific params
        logger.info(
            f"LiteLLMClient initialized for model: {self.model} "
            f"with default kwargs: {self.default_kwargs} "
            f"and model-specific parameters: {self.model_parameters}"
        )

    async def generate_response(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = "auto",
    ) -> LLMOutput:
        """Generates a response using LiteLLM."""
        # Start with default kwargs passed during initialization
        call_kwargs = self.default_kwargs.copy()

        # Find and merge model-specific parameters from config
        reasoning_params_config = None
        for pattern, params in self.model_parameters.items():
            matched = False
            if pattern.endswith("-"): # Prefix match
                if self.model.startswith(pattern[:-1]):
                    matched = True
            elif self.model == pattern: # Exact match
                matched = True

            if matched:
                logger.debug(f"Applying parameters for pattern '{pattern}': {params}")
                # Separate reasoning params if present
                if "reasoning" in params and isinstance(params["reasoning"], dict):
                    reasoning_params_config = params["reasoning"].copy()
                    # Remove reasoning from top-level params to avoid duplication
                    params_copy = params.copy()
                    del params_copy["reasoning"]
                    call_kwargs.update(params_copy) # Merge non-reasoning params
                else:
                    # Merge all params if no 'reasoning' key
                    call_kwargs.update(params)
                # Assuming only one pattern should match, break after first match?
                # Or allow multiple patterns to contribute/override? Let's assume first match wins for now.
                break # Stop after first matching pattern

        # Add model and messages (always required)
        call_kwargs["model"] = self.model
        call_kwargs["messages"] = messages

        # Add tools and tool_choice if tools are provided
        if tools:
            call_kwargs["tools"] = tools
            call_kwargs["tool_choice"] = tool_choice

        # Add the nested 'reasoning' object specifically for OpenRouter models if configured
        if self.model.startswith("openrouter/") and reasoning_params_config:
            call_kwargs["reasoning"] = reasoning_params_config
            logger.debug(f"Adding nested 'reasoning' parameter for OpenRouter: {reasoning_params_config}")

        logger.debug(
            f"Calling LiteLLM model {self.model} with {len(messages)} messages. Tools provided: {bool(tools)}. Final Kwargs: {json.dumps(call_kwargs, default=str)}" # Use json.dumps for better logging
        )
        try:
            response = await acompletion(**call_kwargs)

            # Extract response message
            response_message: Optional[ChatCompletionMessageParam] = (
                response.choices[0].message if response.choices else None
            )

            if not response_message:
                logger.warning(
                    f"LiteLLM response structure unexpected or empty: {response}"
                )
                # Raise an error or return empty output? Let's raise for clarity.
                raise APIError(
                    message="Received empty or unexpected response from LiteLLM.",
                    llm_provider="litellm",
                    model=self.model,
                    status_code=500,
                )  # Simulate server error

            # Extract content, tool calls, and potentially reasoning/usage info
            content = response_message.content
            raw_tool_calls = response_message.tool_calls
            reasoning_info = None
            # Example: Extract usage data if available (common place for token counts)
            if hasattr(response, 'usage') and response.usage:
                try:
                    # Convert Pydantic usage object to dict
                    reasoning_info = response.usage.model_dump(mode="json")
                    logger.debug(f"Extracted usage data as reasoning_info: {reasoning_info}")
                except Exception as usage_err:
                    logger.warning(f"Could not serialize response.usage: {usage_err}")

            # Add other potential reasoning fields if the model/API provides them
            # e.g., if response has a 'debug' or 'reasoning_steps' field:
            # if hasattr(response, 'reasoning_steps'):
            #     if reasoning_info is None: reasoning_info = {}
            #     reasoning_info['steps'] = response.reasoning_steps

            # Convert LiteLLM ToolCall objects to simple dicts for the LLMOutput
            tool_calls_list = []
            if raw_tool_calls:
                for tc in raw_tool_calls:
                    # Assuming tc is a Pydantic model like ToolCall
                    tool_calls_list.append(
                        tc.model_dump(mode="json")
                    )  # Use model_dump for pydantic v2+

            logger.debug(
                f"LiteLLM response received. Content: {bool(content)}. Tool Calls: {len(tool_calls_list)}. Reasoning: {bool(reasoning_info)}"
            )
            return LLMOutput(
                content=content,
                tool_calls=tool_calls_list if tool_calls_list else None,
                reasoning_info=reasoning_info # Pass extracted reasoning info
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


class RecordingLLMClient:
    """
    An LLM client wrapper that records interactions (inputs and outputs)
    to a file while proxying calls to another LLM client.
    """

    def __init__(self, wrapped_client: LLMInterface, recording_path: str):
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
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = "auto",
    ) -> LLMOutput:
        """Calls the wrapped client, records the interaction, and returns the result."""
        # Prepare input data for recording
        input_data = {
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
        }

        try:
            # Call the actual LLM client
            output_data = await self.wrapped_client.generate_response(
                messages=messages, tools=tools, tool_choice=tool_choice
            )

            # Record the interaction (input and output)
            record = {
                "input": input_data,
                "output": output_data.__dict__,
            }  # Use __dict__ for simple dataclass serialization
            try:
                async with aiofiles.open(
                    self.recording_path, mode="a", encoding="utf-8"
                ) as f:
                    await f.write(json.dumps(record, ensure_ascii=False) + "\n")
                logger.debug(f"Recorded interaction to {self.recording_path}")
            except Exception as file_err:
                # Log error but don't fail the LLM call itself
                logger.error(
                    f"Failed to write interaction to recording file {self.recording_path}: {file_err}",
                    exc_info=True,
                )

            return output_data

        except Exception as e:
            # Log the error before re-raising
            logger.error(
                f"Error during wrapped LLM call in RecordingLLMClient: {e}",
                exc_info=True,
            )
            # Optionally record the error state?
            # record = {"input": input_data, "error": str(e)}
            # async with aiofiles.open(self.recording_path, mode="a", encoding="utf-8") as f:
            #     await f.write(json.dumps(record, ensure_ascii=False) + "\n")
            raise  # Re-raise the exception caught from the wrapped client


class PlaybackLLMClient:
    """
    An LLM client that plays back previously recorded interactions from a file.
    Plays back recorded interactions by matching the input arguments.
    """

    def __init__(self, recording_path: str):
        """
        Initializes the playback client by loading all recorded interactions.

        Args:
            recording_path: Path to the JSON Lines file containing recorded interactions.

        Raises:
            FileNotFoundError: If the recording file does not exist.
            ValueError: If the recording file is empty or contains invalid JSON.
        """
        self.recording_path = recording_path
        self.recorded_interactions: List[Dict[str, Any]] = []
        logger.info(
            f"PlaybackLLMClient initializing. Reading from: {self.recording_path}"
        )
        try:
            # Load all interactions into memory synchronously during init
            # For async loading, this would need to be an async factory or method
            with open(self.recording_path, mode="r", encoding="utf-8") as f:
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
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = "auto",
    ) -> LLMOutput:
        """
        Finds a recorded interaction matching the input arguments and returns its output.

        Raises:
            LookupError: If no matching recorded interaction is found for the given input.
        """
        current_input = {
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
        }
        logger.debug(
            f"PlaybackLLMClient attempting to find match for input: {json.dumps(current_input, indent=2)[:500]}..."
        )  # Log truncated input

        # Iterate through loaded interactions to find a match
        for record in self.recorded_interactions:
            # Simple direct comparison of the input dictionaries.
            # NOTE: This can be brittle if there are minor variations (e.g., timestamps in system prompts, dict key order).
            # Consider more robust matching (e.g., comparing specific fields, canonical serialization) if needed.
            if record.get("input") == current_input:
                logger.info(f"Found matching interaction in {self.recording_path}.")
                output_data = record["output"]
                # Reconstruct LLMOutput from the recorded dict
                matched_output = LLMOutput(
                    content=output_data.get("content"),
                    tool_calls=output_data.get("tool_calls"),
                )
                logger.debug(
                    f"Playing back matched response. Content: {bool(matched_output.content)}. Tool Calls: {len(matched_output.tool_calls) if matched_output.tool_calls else 0}"
                )
                return matched_output

        # If no match is found after checking all records
        logger.error(
            f"PlaybackLLMClient: No matching interaction found in {self.recording_path} for the provided input."
        )
        # Log the input that failed to match for debugging
        try:
            failed_input_str = json.dumps(current_input, indent=2)
        except Exception:
            failed_input_str = str(
                current_input
            )  # Fallback if JSON serialization fails
        logger.error(f"Failed Input:\n{failed_input_str}")

        raise LookupError(
            f"No matching recorded interaction found in {self.recording_path} for the current input."
        )
