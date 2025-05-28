"""
Mock LLM implementations for testing purposes.
"""

import logging
from collections.abc import Callable
from typing import Any

from family_assistant.llm import LLMInterface, LLMOutput

logger = logging.getLogger(__name__)

# --- Generic Rule-Based Mock LLM Implementation ---

# Define type aliases for clarity
# MatcherArgs represents the keyword arguments passed to the LLM method
MatcherArgs = dict[str, Any]
# MatcherFunction now takes a single dictionary of arguments,
# which are the keyword arguments for the `generate_response` method.
MatcherFunction = Callable[[MatcherArgs], bool]
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
        logger.info(
            f"RuleBasedMockLLMClient initialized with {len(rules)} rules for model '{self.model}'."
        )

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

        logger.debug(
            f"RuleBasedMockLLM (generate_response) evaluating {len(self.rules)} rules..."
        )
        # The matcher directly receives the kwargs for generate_response.
        for i, (matcher, response) in enumerate(self.rules):
            try:
                # Matcher function now only expects actual_kwargs
                if matcher(actual_kwargs):
                    # Log type of response before accessing attributes
                    logger.debug(
                        f"Rule {i + 1} matched. Response object type: {type(response)}"
                    )

                    logger.info(
                        f"Rule {i + 1} matched for 'generate_response'. Returning predefined response."
                    )
                    logger.debug(
                        f" -> Response Content: {bool(response.content)}, Tool Calls: {len(response.tool_calls) if response.tool_calls else 0}"
                    )
                    return response
            except Exception as e:
                # Clarify if error was in matcher or response processing if possible,
                # but exc_info=True will give the most direct traceback.
                logger.error(
                    f"Error processing rule {i + 1} (matcher or response callable) for 'generate_response': {e}",
                    exc_info=True,
                )
                # Continue to next rule or default if matcher/response itself fails
                continue

        logger.warning(
            "No rules matched for 'generate_response'. Returning default response. Input kwargs: %r",
            actual_kwargs,
        )
        return self.default_response

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
    ) -> dict[str, Any]:
        """
        Mock implementation for formatting a user message with file.
        This mock provides a direct, non-rule-based implementation.
        """
        import os  # For os.path.basename

        actual_kwargs: MatcherArgs = {
            "prompt_text": prompt_text,
            "file_path": file_path,
            "mime_type": mime_type,
            "max_text_length": max_text_length,
            # No "_method_name_for_matcher" here as this method isn't using the rule system
        }
        self._record_call("format_user_message_with_file", actual_kwargs)

        content_parts: list[dict[str, Any]] = []
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

        user_message_content: str | list[dict[str, Any]]
        if len(content_parts) == 1 and content_parts[0]["type"] == "text":
            user_message_content = content_parts[0]["text"]
        else:
            user_message_content = content_parts

        return {"role": "user", "content": user_message_content}


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


def get_system_prompt(messages: list[dict[str, Any]]) -> str | None:
    """Extracts the system prompt content from a list of messages."""
    if not messages:
        return None
    if messages[0].get("role") == "system":
        content = messages[0].get("content")
        if isinstance(content, str):
            return content
    return None


__all__ = [
    "RuleBasedMockLLMClient",
    "Rule",
    "MatcherFunction",
    "get_last_message_text",
    "get_system_prompt",
]
