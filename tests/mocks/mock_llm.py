"""
Mock LLM implementations for testing purposes.
"""

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Callable, Sequence
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolAttachment

from family_assistant.llm import (
    BaseLLMClient,
    JsonObject,
    LLMInterface,
    LLMMessage,
    LLMOutput,
    LLMStreamEvent,
    StreamEventMetadata,
    StructuredOutputError,
    UserMessageContentPart,
    UserMessageDict,
)
from family_assistant.llm.messages import (
    UserMessage,
    is_turn_scaffolding,
    message_to_json_dict,
)
from family_assistant.storage.database import in_transaction
from family_assistant.tools.types import ToolDefinition

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger(__name__)

# --- Generic Rule-Based Mock LLM Implementation ---

# Define type aliases for clarity
# MatcherArgs represents the keyword arguments passed to the LLM method
# ast-grep-ignore: no-dict-any - type alias for dynamic LLM matcher kwargs
MatcherArgs = dict[str, Any]
# MatcherFunction now takes a single dictionary of arguments,
# which are the keyword arguments for the `generate_response` method.
MatcherFunction = Callable[[MatcherArgs], bool]
# ResponseGenerator can be either a static LLMOutput or a callable that returns LLMOutput
ResponseGenerator = LLMOutput | Callable[[MatcherArgs], LLMOutput]
Rule = tuple[MatcherFunction, ResponseGenerator]

# Type aliases for structured output rules
# StructuredMatcherArgs includes the response_model type
# ast-grep-ignore: no-dict-any - type alias for dynamic structured LLM matcher kwargs
StructuredMatcherArgs = dict[str, Any]
# StructuredResponseGenerator can return any BaseModel subclass
StructuredResponseGenerator = BaseModel | Callable[[StructuredMatcherArgs], BaseModel]
StructuredRule = tuple[MatcherFunction, StructuredResponseGenerator]


class RuleBasedMockLLMClient(BaseLLMClient, LLMInterface):
    """
    A mock LLM client that responds based on a list of predefined rules.
    Each rule consists of a matcher function and the LLMOutput to return if matched.
    Rules are evaluated in the order they are provided.
    The matcher function receives the method name and a dictionary of all keyword arguments.

    Inherits from BaseLLMClient to get common functionality like empty input validation.
    """

    def __init__(
        self,
        rules: list[Rule],
        default_response: LLMOutput | None = None,
        model_name: str = "mock-llm-model",
        structured_rules: list[StructuredRule] | None = None,
        response_gate: asyncio.Event | None = None,
    ) -> None:
        """
        Initializes the mock client with rules.

        Args:
            rules: A list of tuples, where each tuple contains:
                   - A matcher function: Takes (method_name, kwargs_dict) and returns True if the rule matches.
                   - An LLMOutput object: The response to return if the matcher is True.
            default_response: An optional LLMOutput to return if no rules match.
                              If None, a basic default response is used.
            model_name: A name for this mock model, can be used by processors.
            structured_rules: Optional list of rules for generate_structured method.
                              Each rule is a tuple of (matcher_function, response_generator).
                              The response_generator should return a Pydantic model instance.
            response_gate: Optional asyncio.Event the test injects to hold replies
                           in flight. generate_response awaits it before producing
                           a reply; the test calls set() to release. Defaults to
                           None (no gating).
        """
        self.rules: list[Rule] = list(rules)
        self.structured_rules: list[StructuredRule] = list(structured_rules or [])
        self.model = model_name  # For getattr(llm_client, "model", "unknown")
        # Optional injected gate: when provided, generate_response awaits this
        # event before producing a reply, letting a test hold a response "in
        # flight" (e.g. to observe a non-terminal task or cancel mid-run) using
        # the fake's own API rather than monkeypatching it.
        self.response_gate: asyncio.Event | None = response_gate
        if default_response is None:
            self.default_response = LLMOutput(
                content="Sorry, no matching rule was found for this input in the mock.",
                tool_calls=None,
            )
            logger.debug("RuleBasedMockLLMClient using default fallback response.")
        else:
            self.default_response = default_response
            logger.debug("RuleBasedMockLLMClient using provided default response.")

        # ast-grep-ignore: no-dict-any - records calls with mixed-type kwargs
        self._calls: list[dict[str, Any]] = []
        logger.info(
            f"RuleBasedMockLLMClient initialized with {len(rules)} rules for model '{self.model}'."
        )

    # ast-grep-ignore: no-dict-any - LLM kwargs contain mixed provider-specific fields
    def _record_call(self, method_name: str, actual_kwargs: dict[str, Any]) -> None:
        """Helper to store call data."""
        # No transaction may span an LLM call: it would hold a connection (and,
        # on SQLite, the engine lock) for the length of a model round trip.
        # Asserting it here makes the architectural rule a failing test.
        assert not in_transaction(), (
            f"LLM call '{method_name}' was made with a database transaction open"
        )
        call_data = {
            "method_name": method_name,
            "kwargs": actual_kwargs,
        }
        self._calls.append(call_data)
        logger.debug(
            f"Recorded call to '{method_name}'. Total calls: {len(self._calls)}. Args: {actual_kwargs}"
        )

    # ast-grep-ignore: no-dict-any - returns recorded calls with mixed-type kwargs
    def get_calls(self) -> list[dict[str, Any]]:
        """Returns a list of recorded calls."""
        return self._calls

    async def generate_response(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """
        Evaluates rules against the input for 'generate_response' and returns the corresponding output.
        """
        # Validate user input before processing
        self._validate_user_input(messages)

        # Hold the response in flight until released, if a gate is configured.
        if self.response_gate is not None:
            await self.response_gate.wait()

        # Pass typed messages directly to matchers
        # Matchers can access fields via .attribute or dict-style .get()
        actual_kwargs: MatcherArgs = {
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
        }
        self._record_call("generate_response", actual_kwargs)

        logger.debug(
            f"RuleBasedMockLLM (generate_response) evaluating {len(self.rules)} rules..."
        )
        # The matcher directly receives the kwargs for generate_response.
        for i, (matcher, response_generator) in enumerate(self.rules):
            try:
                # Matcher function now only expects actual_kwargs
                if matcher(actual_kwargs):
                    actual_response_output: LLMOutput
                    if callable(response_generator):
                        logger.debug(
                            f"Rule {i + 1} matched. Response generator is callable ({type(response_generator).__name__}). Calling it."
                        )
                        # If the response_generator is a callable, call it to get the LLMOutput
                        actual_response_output = response_generator(actual_kwargs)
                    else:
                        # If it's not callable, assume it's already an LLMOutput instance
                        actual_response_output = response_generator

                    # Log type of actual_response_output before accessing attributes
                    logger.debug(
                        f"Rule {i + 1} matched. Actual response output type: {type(actual_response_output)}"
                    )

                    # Corrected logging to reflect that the response is now generated/retrieved
                    log_message_action = (
                        "Returning generated response from callable."
                        if callable(response_generator)
                        else "Returning predefined response object."
                    )
                    logger.info(
                        f"Rule {i + 1} matched for 'generate_response'. {log_message_action}"
                    )
                    tool_names = (
                        [tc.function.name for tc in actual_response_output.tool_calls]
                        if actual_response_output.tool_calls
                        else []
                    )
                    content_preview = (actual_response_output.content or "")[:50]
                    logger.info(
                        f" -> Mock LLM returning: content='{content_preview}...', "
                        f"tool_calls={len(actual_response_output.tool_calls) if actual_response_output.tool_calls else 0}, "
                        f"tool_names={tool_names}"
                    )
                    return actual_response_output
            except Exception as e:
                # Clarify if error was in matcher or response processing if possible,
                # but the logged traceback gives the most direct signal.
                logger.exception(
                    f"Error processing rule {i + 1} (matcher or response callable) for 'generate_response': {e}"
                )
                # Continue to next rule or default if matcher/response itself fails
                continue

        # Enhanced logging for debugging test failures
        logger.warning(
            f"No mock LLM rule matched for 'generate_response' with {len(messages)} messages. "
            f"Returning default response."
        )
        # Convert messages to dicts and sanitize for JSON logging
        sanitized_messages = []
        for msg in messages:
            msg_dict = message_to_json_dict(msg)
            msg_dict.pop("_attachments", None)
            sanitized_messages.append(msg_dict)
        logger.warning(
            f"Messages received:\n{json.dumps(sanitized_messages, indent=2)}"
        )
        return self.default_response

    def generate_response_stream(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Mock streaming implementation that yields events based on generate_response."""
        # Validate user input before processing
        self._validate_user_input(messages)

        async def _stream() -> AsyncIterator[LLMStreamEvent]:
            # Get the non-streaming response
            response = await self.generate_response(messages, tools, tool_choice)

            # Yield content in chunks if present
            if response.content:
                # Split content into words and yield them
                words = response.content.split()
                for i, word in enumerate(words):
                    # Add space before word unless it's the first word
                    chunk = word if i == 0 else f" {word}"
                    yield LLMStreamEvent(type="content", content=chunk)
                    # Add a small delay to simulate streaming
                    # ast-grep-ignore: no-asyncio-sleep-in-tests - Simulating LLM streaming delay
                    await asyncio.sleep(0.01)

            # Yield tool calls if present
            if response.tool_calls:
                for tool_call in response.tool_calls:
                    yield LLMStreamEvent(type="tool_call", tool_call=tool_call)

            # Yield done event with metadata
            done_metadata: StreamEventMetadata = {}
            if response.reasoning_info:
                done_metadata["reasoning_info"] = response.reasoning_info
            if response.resolved_model:
                done_metadata["resolved_model"] = response.resolved_model
            yield LLMStreamEvent(type="done", metadata=done_metadata)

        return _stream()

    def create_attachment_injection(
        self,
        attachment: "ToolAttachment",
    ) -> UserMessage:
        """
        Mock implementation of attachment injection for testing.
        Creates a simple user message with attachment information.
        """
        content = "[System: File from previous tool response]\n"
        if hasattr(attachment, "description") and attachment.description:
            content += f"[Description: {attachment.description}]\n"
        if hasattr(attachment, "attachment_id") and attachment.attachment_id:
            content += f"[Attachment ID: {attachment.attachment_id}]\n"
        if hasattr(attachment, "mime_type") and attachment.mime_type:
            content += f"[MIME Type: {attachment.mime_type}]\n"
        if hasattr(attachment, "content") and attachment.content:
            content_size = len(attachment.content)
            content += f"[Size: {content_size} bytes]\n"

        return UserMessage(content=content)

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
    ) -> UserMessageDict:
        """
        Mock implementation for formatting a user message with file.
        This mock provides a direct, non-rule-based implementation.
        """

        actual_kwargs: MatcherArgs = {
            "prompt_text": prompt_text,
            "file_path": file_path,
            "mime_type": mime_type,
            "max_text_length": max_text_length,
        }
        self._record_call("format_user_message_with_file", actual_kwargs)

        content_parts: list[UserMessageContentPart] = []
        final_prompt_text = prompt_text or "Process the provided file."

        if (
            max_text_length
            and file_path is None
            and len(final_prompt_text) > max_text_length
        ):
            final_prompt_text = final_prompt_text[:max_text_length]

        content_parts.append({"type": "text", "text": final_prompt_text})

        if file_path and mime_type:
            if mime_type.startswith("image/"):
                content_parts.append({
                    "type": "image_url",
                    "image_url": {
                        "url": (
                            f"data:{mime_type};base64,mock_image_data_for_{os.path.basename(file_path)}"
                        )
                    },
                })
            else:  # Generic file
                content_parts.append({
                    "type": "file_placeholder",  # Custom type for mock
                    "file_reference": {
                        "file_path": file_path,
                        "mime_type": mime_type,
                        "description": "This is a mock file reference.",
                    },
                })

        user_message_content: str | list[UserMessageContentPart]
        if len(content_parts) == 1 and content_parts[0]["type"] == "text":
            user_message_content = content_parts[0]["text"]
        else:
            user_message_content = content_parts

        return {"role": "user", "content": user_message_content}

    async def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
        max_retries: int = 2,
    ) -> T:
        """
        Mock implementation of generate_structured for testing.

        Uses structured_rules if configured, otherwise falls back to default behavior.
        The mock can be configured with structured_rules that return Pydantic model instances.
        """
        actual_kwargs: StructuredMatcherArgs = {
            "messages": list(messages),
            "response_model": response_model,
            "response_model_name": response_model.__name__,
            "max_retries": max_retries,
        }
        self._record_call("generate_structured", actual_kwargs)

        logger.debug(
            f"RuleBasedMockLLM (generate_structured) evaluating {len(self.structured_rules)} structured rules..."
        )

        for i, (matcher, response_generator) in enumerate(self.structured_rules):
            try:
                if matcher(actual_kwargs):
                    actual_response: BaseModel
                    # Check if it's a static BaseModel instance or a callable
                    if isinstance(response_generator, BaseModel):
                        actual_response = response_generator
                    else:
                        # It's a callable that returns a BaseModel
                        logger.debug(
                            f"Structured rule {i + 1} matched. Response generator is callable. Calling it."
                        )
                        actual_response = response_generator(actual_kwargs)

                    logger.info(
                        f"Structured rule {i + 1} matched for 'generate_structured'. "
                        f"Returning {type(actual_response).__name__}."
                    )

                    # Validate that the response matches the expected model type
                    if not isinstance(actual_response, response_model):
                        raise StructuredOutputError(
                            message=(
                                f"Mock returned {type(actual_response).__name__} but "
                                f"expected {response_model.__name__}"
                            ),
                            provider="mock",
                            model=self.model,
                            raw_response=str(actual_response),
                            validation_error=None,
                        )

                    # Type is validated by isinstance check above
                    return actual_response  # type: ignore[return-value] # validated by isinstance

            except StructuredOutputError:
                raise
            except Exception as e:
                logger.exception(f"Error processing structured rule {i + 1}: {e}")
                continue

        # No rule matched - raise an error (unlike generate_response which has a default)
        logger.warning(
            f"No mock LLM structured rule matched for 'generate_structured' "
            f"with model {response_model.__name__}."
        )
        raise StructuredOutputError(
            message=f"No matching structured rule found for model {response_model.__name__}",
            provider="mock",
            model=self.model,
            raw_response=None,
            validation_error=None,
        )

    async def generate_json(
        self,
        messages: Sequence[LLMMessage],
        max_retries: int = 2,
    ) -> JsonObject:
        """Mock implementation of generate_json for testing."""
        actual_kwargs: MatcherArgs = {
            "messages": list(messages),
            "max_retries": max_retries,
        }
        self._record_call("generate_json", actual_kwargs)

        response = await self.generate_response(list(messages))
        if not response.content:
            raise StructuredOutputError(
                message="Mock returned empty response for generate_json",
                provider="mock",
                model=self.model,
                raw_response=None,
                validation_error=None,
            )

        try:
            parsed = json.loads(self._extract_json_from_response(response.content))
        except json.JSONDecodeError as e:
            raise StructuredOutputError(
                message="Mock returned invalid JSON for generate_json",
                provider="mock",
                model=self.model,
                raw_response=response.content,
                validation_error=e,
            ) from e

        if not isinstance(parsed, dict):
            raise StructuredOutputError(
                message="Mock returned JSON that was not a JSON object",
                provider="mock",
                model=self.model,
                raw_response=response.content,
                validation_error=None,
            )

        return parsed


# --- Helper functions for working with typed messages in matchers ---


def get_message_role(msg: LLMMessage) -> str:
    """Get the role from a typed message or dict."""
    if isinstance(msg, dict):
        return msg.get("role", "")
    return msg.role


def get_message_content(msg: LLMMessage) -> str | list | None:
    """Get the content from a typed message or dict."""
    if isinstance(msg, dict):
        return msg.get("content")
    return msg.content


def extract_text_from_content(content: str | list | None) -> str:
    """Extract text string from message content (handles both string and multi-part)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Extract text from multi-part content (ContentPart objects or dicts)
        text_parts = []
        for part in content:
            # Handle both Pydantic ContentPart objects and dict representations
            if isinstance(part, dict):
                # Dict representation: {"type": "text", "text": "..."}
                if part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
            elif hasattr(part, "type") and part.type == "text":
                # Pydantic ContentPart object
                text_parts.append(part.text)
        return " ".join(text_parts).strip()
    return ""


# --- Helper function to extract text from messages ---
# (Useful for writing matchers)
def last_real_message(messages: list[LLMMessage]) -> LLMMessage | None:
    """The newest message that is not turn scaffolding, or None.

    Tests that want "the message the turn is about" need this rather than
    ``messages[-1]``: the prompt ends with the ``<turn_context>`` block, and on a
    final iteration with the loop's instruction after that.
    """
    return next(
        (msg for msg in reversed(messages) if not is_turn_scaffolding(msg)), None
    )


def get_last_message_text(messages: list[LLMMessage]) -> str:
    """Extracts and concatenates text from the last message in a list.

    Turn scaffolding is skipped, so matchers keyed on "what was just asked" see
    the request rather than the trailing ``<turn_context>`` block or the
    final-iteration instruction. This mirrors the real loop, which skips the
    same messages when it looks back for the user's query.
    """
    last_message = last_real_message(messages)
    if last_message is None:
        return ""

    return extract_text_from_content(get_message_content(last_message))


def get_system_prompt(messages: list[LLMMessage]) -> str | None:
    """Extracts the system prompt content from a list of messages."""
    if not messages:
        return None

    first_msg = messages[0]

    if get_message_role(first_msg) == "system":
        content = get_message_content(first_msg)
        if isinstance(content, str):
            return content
    return None


__all__ = [
    "MatcherFunction",
    "Rule",
    "RuleBasedMockLLMClient",
    "StructuredResponseGenerator",
    "StructuredRule",
    "extract_text_from_content",
    "get_last_message_text",
    "get_message_content",
    "get_message_role",
    "get_system_prompt",
    "last_real_message",
]
