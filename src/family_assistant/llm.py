"""
Module defining the interface and implementations for interacting with Large Language Models (LLMs).
"""

import json
import logging
import os
import aiofiles  # For async file operations
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Protocol, AsyncGenerator, AsyncIterator

from litellm import acompletion
from litellm.exceptions import APIConnectionError, Timeout, RateLimitError, ServiceUnavailableError, APIError
from litellm.types.completion import ChatCompletionMessageParam, ChatCompletionToolParam

logger = logging.getLogger(__name__)


@dataclass
class LLMOutput:
    """Standardized output structure from an LLM call."""
    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = field(default_factory=list) # Store raw tool call dicts


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

    def __init__(self, model: str, **kwargs: Any):
        """
        Initializes the LiteLLM client.

        Args:
            model: The identifier of the model to use (e.g., "openrouter/google/gemini-flash-1.5").
            **kwargs: Additional keyword arguments to pass directly to litellm.acompletion
                      on every call (e.g., temperature, max_tokens).
        """
        if not model:
            raise ValueError("LLM model identifier cannot be empty.")
        self.model = model
        self.completion_kwargs = kwargs
        logger.info(f"LiteLLMClient initialized for model: {self.model} with kwargs: {self.completion_kwargs}")

    async def generate_response(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = "auto",
    ) -> LLMOutput:
        """Generates a response using LiteLLM."""
        effective_tool_choice = tool_choice if tools else None # Don't specify tool_choice if no tools are given

        logger.debug(f"Calling LiteLLM model {self.model} with {len(messages)} messages. Tools provided: {bool(tools)}. Tool choice: {effective_tool_choice}")
        try:
            # Combine fixed kwargs with per-call args
            call_kwargs = {
                **self.completion_kwargs,
                "model": self.model,
                "messages": messages,
                "tools": tools,
                "tool_choice": effective_tool_choice,
            }
            response = await acompletion(**call_kwargs)

            # Extract response message
            response_message: Optional[ChatCompletionMessageParam] = (
                response.choices[0].message if response.choices else None
            )

            if not response_message:
                logger.warning(f"LiteLLM response structure unexpected or empty: {response}")
                # Raise an error or return empty output? Let's raise for clarity.
                raise APIError("Received empty or unexpected response from LiteLLM.", status_code=500) # Simulate server error

            # Extract content and tool calls
            content = response_message.content
            raw_tool_calls = response_message.tool_calls

            # Convert LiteLLM ToolCall objects to simple dicts for the LLMOutput
            tool_calls_list = []
            if raw_tool_calls:
                for tc in raw_tool_calls:
                    # Assuming tc is a Pydantic model like ToolCall
                    tool_calls_list.append(tc.model_dump(mode='json')) # Use model_dump for pydantic v2+

            logger.debug(f"LiteLLM response received. Content: {bool(content)}. Tool Calls: {len(tool_calls_list)}")
            return LLMOutput(content=content, tool_calls=tool_calls_list if tool_calls_list else None)

        except (APIConnectionError, Timeout, RateLimitError, ServiceUnavailableError, APIError) as e:
            logger.error(f"LiteLLM API error for model {self.model}: {e}", exc_info=True)
            raise  # Re-raise the specific LiteLLM exception
        except Exception as e:
            logger.error(f"Unexpected error during LiteLLM call for model {self.model}: {e}", exc_info=True)
            # Wrap unexpected errors in a generic APIError or a custom exception
            raise APIError(f"Unexpected error: {e}", status_code=500) from e


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
        if not hasattr(wrapped_client, 'generate_response'):
             raise TypeError("wrapped_client must implement the LLMInterface protocol.")
        self.wrapped_client = wrapped_client
        self.recording_path = recording_path
        # Ensure directory exists (optional, depends on desired behavior)
        os.makedirs(os.path.dirname(self.recording_path), exist_ok=True)
        logger.info(f"RecordingLLMClient initialized. Wrapping {type(wrapped_client).__name__}. Recording to: {self.recording_path}")

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
            record = {"input": input_data, "output": output_data.__dict__} # Use __dict__ for simple dataclass serialization
            try:
                async with aiofiles.open(self.recording_path, mode="a", encoding="utf-8") as f:
                    await f.write(json.dumps(record, ensure_ascii=False) + "\n")
                logger.debug(f"Recorded interaction to {self.recording_path}")
            except Exception as file_err:
                # Log error but don't fail the LLM call itself
                logger.error(f"Failed to write interaction to recording file {self.recording_path}: {file_err}", exc_info=True)

            return output_data

        except Exception as e:
            # Log the error before re-raising
            logger.error(f"Error during wrapped LLM call in RecordingLLMClient: {e}", exc_info=True)
            # Optionally record the error state?
            # record = {"input": input_data, "error": str(e)}
            # async with aiofiles.open(self.recording_path, mode="a", encoding="utf-8") as f:
            #     await f.write(json.dumps(record, ensure_ascii=False) + "\n")
            raise # Re-raise the exception caught from the wrapped client


class PlaybackLLMClient:
    """
    An LLM client that plays back previously recorded interactions from a file.
    Plays back interactions sequentially based on the order in the file.
    """

    def __init__(self, recording_path: str):
        """
        Initializes the playback client.

        Args:
            recording_path: Path to the JSON Lines file containing recorded interactions.
        """
        self.recording_path = recording_path
        self._iterator: Optional[AsyncIterator[LLMOutput]] = None
        logger.info(f"PlaybackLLMClient initialized. Reading from: {self.recording_path}")

    async def _load_recording(self) -> AsyncGenerator[LLMOutput, None]:
        """Async generator to load recorded outputs one by one."""
        try:
            async with aiofiles.open(self.recording_path, mode="r", encoding="utf-8") as f:
                line_num = 0
                async for line in f:
                    line_num += 1
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        if "output" not in record:
                             logger.warning(f"Skipping line {line_num} in {self.recording_path}: Missing 'output' key.")
                             continue
                        # Reconstruct LLMOutput from the recorded dict
                        output_data = record["output"]
                        yield LLMOutput(
                            content=output_data.get("content"),
                            tool_calls=output_data.get("tool_calls")
                        )
                    except json.JSONDecodeError:
                        logger.warning(f"Skipping invalid JSON on line {line_num} in {self.recording_path}: {line[:100]}...")
                    except Exception as parse_err:
                         logger.warning(f"Error parsing record on line {line_num} in {self.recording_path}: {parse_err}")
        except FileNotFoundError:
            logger.error(f"Recording file not found: {self.recording_path}")
            raise # Re-raise FileNotFoundError
        except Exception as e:
            logger.error(f"Failed to read or parse recording file {self.recording_path}: {e}", exc_info=True)
            raise # Re-raise other critical errors

    async def generate_response(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = "auto",
    ) -> LLMOutput:
        """Returns the next recorded response from the file."""
        if self._iterator is None:
            self._iterator = self._load_recording()

        try:
            # Get the next recorded output sequentially
            recorded_output = await self._iterator.__anext__()
            logger.debug(f"Playing back recorded response. Content: {bool(recorded_output.content)}. Tool Calls: {len(recorded_output.tool_calls) if recorded_output.tool_calls else 0}")
            # TODO: Add optional input matching logic here if needed in the future
            # For now, just return the next item regardless of input args.
            return recorded_output
        except StopAsyncIteration:
            logger.error(f"PlaybackLLMClient: Reached end of recording file {self.recording_path}.")
            raise EOFError(f"End of recording file reached: {self.recording_path}")
        except Exception as e:
            logger.error(f"Error retrieving next item during playback: {e}", exc_info=True)
            raise # Re-raise other errors encountered during iteration
