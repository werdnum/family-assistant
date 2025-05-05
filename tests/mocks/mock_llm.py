"""
Mock LLM implementations for testing purposes.
"""

import json
import logging
import uuid
from typing import List, Dict, Any, Optional, Callable, Tuple

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
            content: Optional[str] = None,
            tool_calls: Optional[List[Dict[str, Any]]] = None,
        ):
            self.content = content
            self.tool_calls = tool_calls or []

    class LLMInterface(Protocol):
        """Mock version of LLMInterface for when the real one can't be imported"""

        async def generate_response(
            self,
            messages: List[Dict[str, Any]],
            tools: Optional[List[Dict[str, Any]]] = None,
            tool_choice: Optional[str] = "auto",
        ) -> LLMOutput: ...


logger = logging.getLogger(__name__)

# --- Generic Rule-Based Mock LLM Implementation ---

# Define type aliases for clarity
# Matcher takes (messages, tools, tool_choice) -> bool
MatcherFunction = Callable[
    [List[Dict[str, Any]], Optional[List[Dict[str, Any]]], Optional[str]], bool
]
Rule = Tuple[MatcherFunction, LLMOutput]


class RuleBasedMockLLMClient(LLMInterface):
    """
    A mock LLM client that responds based on a list of predefined rules.
    Each rule consists of a matcher function and the LLMOutput to return if matched.
    Rules are evaluated in the order they are provided.
    """

    def __init__(self, rules: List[Rule], default_response: Optional[LLMOutput] = None):
        """
        Initializes the mock client with rules.

        Args:
            rules: A list of tuples, where each tuple contains:
                   - A matcher function: Takes (messages, tools, tool_choice) and returns True if the rule matches.
                   - An LLMOutput object: The response to return if the matcher is True.
            default_response: An optional LLMOutput to return if no rules match.
                              If None, a basic default response is used.
        """
        self.rules = rules
        if default_response is None:
            self.default_response = LLMOutput(
                content="Sorry, no matching rule was found for this input in the mock.",
                tool_calls=None,
            )
            logger.debug("RuleBasedMockLLMClient using default fallback response.")
        else:
            self.default_response = default_response
            logger.debug("RuleBasedMockLLMClient using provided default response.")
        # Add call recording
        self._calls: List[Dict[str, Any]] = []
        self.generate_response = self._generate_response_wrapper(self.generate_response) # Wrap for recording
        logger.info(f"RuleBasedMockLLMClient initialized with {len(rules)} rules.")

    async def generate_response(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = "auto",
    ) -> LLMOutput:
        """
        Evaluates rules against the input and returns the corresponding output.
        """
        # Original logic starts here, this method will be wrapped
        # Record the call *before* executing logic
        call_data = {
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
        }
        # We'll record the call in the wrapper instead
        # self._calls.append(call_data)
        # logger.debug(f"Recorded call {self.call_count()}. Args: {call_data}")

        logger.debug(f"RuleBasedMockLLM evaluating {len(self.rules)} rules...")
        for i, (matcher, response) in enumerate(self.rules):
            try:
                if matcher(messages, tools, tool_choice):
                    logger.info(f"Rule {i+1} matched. Returning predefined response.")
                    # Maybe add logging of the response content/tools here if needed
                    logger.debug(
                        f" -> Response Content: {bool(response.content)}, Tool Calls: {len(response.tool_calls) if response.tool_calls else 0}"
                    )
                    return response
            except Exception as e:
                logger.error(
                    f"Error executing matcher for rule {i+1}: {e}", exc_info=True
                )
                # Decide how to handle matcher errors: skip rule, raise, etc.
                # Skipping seems reasonable for a mock.
                continue  # Skip to the next rule

        # If no rules matched
        logger.warning(
            f"No rules matched the input. Returning default response. Input: %r",
            messages,
        )
        return self.default_response

    # Wrapper to record calls
    def _generate_response_wrapper(self, original_method):
        async def wrapper(*args, **kwargs):
            # Positional args: self, messages
            # Keyword args: tools, tool_choice
            messages = args[1] if len(args) > 1 else kwargs.get("messages")
            tools = kwargs.get("tools")
            tool_choice = kwargs.get("tool_choice", "auto")

            call_data = {
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
            }
            self._calls.append(call_data)
            logger.debug(f"Recorded call {len(self._calls)}. Args keys: {call_data.keys()}")
            return await original_method(*args, **kwargs)
        return wrapper


# --- Helper function to extract text from messages ---
# (Useful for writing matchers)
def get_last_message_text(messages: List[Dict[str, Any]]) -> str:
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
