"""
Mock LLM implementations for testing purposes.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

# Assuming LLMInterface and LLMOutput are accessible, adjust import if needed
# e.g., from family_assistant.llm import LLMInterface, LLMOutput
# For now, let's define placeholders if the real ones aren't easily importable here
try:
    from family_assistant.llm import LLMInterface, LLMOutput
except ImportError:
    from typing import Protocol

    class LLMOutput:
        """Mock version of LLMOutput for when the real one can't be imported"""

        def __init__(
            self,
            content: str | None = None,
            tool_calls: list[dict[str, Any]] | None = None,
        ) -> None:
            self.content = content
            self.tool_calls = tool_calls or []

    class LLMInterface(Protocol):
        """Mock version of LLMInterface for when the real one can't be imported"""

        async def generate_response(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None = None,
            tool_choice: str | None = "auto",
        ) -> LLMOutput: ...


logger = logging.getLogger(__name__)

# --- Generic Rule-Based Mock LLM Implementation ---

# Define type aliases for clarity
# MatcherArgs represents the keyword arguments passed to the LLM method
MatcherArgs = dict[str, Any]
# MatcherFunction takes (method_name, all_kwargs_of_the_method) -> bool
MatcherFunction = Callable[[str, MatcherArgs], bool]
Rule = tuple[MatcherFunction, LLMOutput]


class RuleBasedMockLLMClient(LLMInterface):
    """
    A mock LLM client that responds based on a list of predefined rules.
    Each rule consists of a matcher function and the LLMOutput to return if matched.
    Rules are evaluated in the order they are provided.
    The matcher function receives the method name and a dictionary of all keyword arguments.
    """

    def __init__(
        self,
        rules: list[Rule],
        default_response: LLMOutput | None = None,
        model_name: str = "mock-llm-model",
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
        """
        self.rules = rules
        self.model = model_name  # For getattr(llm_client, "model", "unknown")
        if default_response is None:
            self.default_response = LLMOutput(
                content="Sorry, no matching rule was found for this input in the mock.",
                tool_calls=None,
            )
            logger.debug("RuleBasedMockLLMClient using default fallback response.")
        else:
            self.default_response = default_response
            logger.debug("RuleBasedMockLLMClient using provided default response.")

        self._calls: list[dict[str, Any]] = []
        logger.info(f"RuleBasedMockLLMClient initialized with {len(rules)} rules for model '{self.model}'.")

    def _record_call(self, method_name: str, actual_kwargs: dict[str, Any]) -> None:
        """Helper to store call data."""
        call_data = {
            "method_name": method_name,
            "kwargs": actual_kwargs,
        }
        self._calls.append(call_data)
        logger.debug(
            f"Recorded call to '{method_name}'. Total calls: {len(self._calls)}. Args: {actual_kwargs}"
        )

    def get_calls(self) -> list[dict[str, Any]]:
        """Returns a list of recorded calls."""
        return self._calls

    async def generate_response(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """
        Evaluates rules against the input for 'generate_response' and returns the corresponding output.
        """
        actual_kwargs: MatcherArgs = {
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
        }
        self._record_call("generate_response", actual_kwargs)

        logger.debug(f"RuleBasedMockLLM (generate_response) evaluating {len(self.rules)} rules...")
        for i, (matcher, response) in enumerate(self.rules):
            try:
                if matcher("generate_response", actual_kwargs):
                    logger.info(f"Rule {i + 1} matched for 'generate_response'. Returning predefined response.")
                    logger.debug(
                        f" -> Response Content: {bool(response.content)}, Tool Calls: {len(response.tool_calls) if response.tool_calls else 0}"
                    )
                    return response
            except Exception as e:
                logger.error(
                    f"Error executing matcher for rule {i + 1} (generate_response): {e}", exc_info=True
                )
                continue

        logger.warning(
            "No rules matched for 'generate_response'. Returning default response. Input kwargs: %r",
            actual_kwargs,
        )
        return self.default_response

    async def generate_response_from_file_input(
        self,
        system_prompt: str,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        tools: list[dict[str, Any]] | None,
        tool_choice: str | None,
        max_text_length: int | None,
    ) -> LLMOutput:
        """
        Evaluates rules against the input for 'generate_response_from_file_input' and returns the corresponding output.
        """
        actual_kwargs: MatcherArgs = {
            "system_prompt": system_prompt,
            "prompt_text": prompt_text,
            "file_path": file_path,
            "mime_type": mime_type,
            "tools": tools,
            "tool_choice": tool_choice,
            "max_text_length": max_text_length,
        }
        self._record_call("generate_response_from_file_input", actual_kwargs)

        logger.debug(f"RuleBasedMockLLM (generate_response_from_file_input) evaluating {len(self.rules)} rules...")
        for i, (matcher, response) in enumerate(self.rules):
            try:
                if matcher("generate_response_from_file_input", actual_kwargs):
                    logger.info(f"Rule {i + 1} matched for 'generate_response_from_file_input'. Returning predefined response.")
                    logger.debug(
                        f" -> Response Content: {bool(response.content)}, Tool Calls: {len(response.tool_calls) if response.tool_calls else 0}"
                    )
                    return response
            except Exception as e:
                logger.error(
                    f"Error executing matcher for rule {i + 1} (generate_response_from_file_input): {e}", exc_info=True
                )
                continue
        
        logger.warning(
            "No rules matched for 'generate_response_from_file_input'. Returning default response. Input kwargs: %r",
            actual_kwargs,
        )
        return self.default_response


# --- Helper function to extract text from messages ---
# (Useful for writing matchers)
def get_last_message_text(messages: list[dict[str, Any]]) -> str:
    """Extracts and concatenates text from the last message in a list."""
    if not messages:
        return ""
    last_message_content = messages[-1].get("content", "")
    if isinstance(last_message_content, list):  # Handle multi-part
        # Ensure part is a dict and has 'type' and 'text' keys before accessing
        text_parts = [
            part.get("text", "")
            for part in last_message_content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return " ".join(text_parts).strip()
    elif isinstance(last_message_content, str):
        return last_message_content.strip()
    # Handle other potential content types gracefully (e.g., None)
    return ""


__all__ = ["RuleBasedMockLLMClient", "Rule", "MatcherFunction", "get_last_message_text"]
