"""
Base components for LLM integration including protocols, data classes, and exceptions.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol


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

        Args:
            messages: List of message dictionaries with 'role' and 'content' keys
            tools: Optional list of tool definitions in OpenAI format
            tool_choice: How to handle tool selection ("auto", "none", etc.)

        Returns:
            LLMOutput containing the response content and/or tool calls
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


# Exception hierarchy for LLM providers
class LLMProviderError(Exception):
    """Base exception for LLM provider errors."""

    def __init__(self, message: str, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model
        super().__init__(message)


class RateLimitError(LLMProviderError):
    """Raised when hitting provider rate limits."""

    pass


class AuthenticationError(LLMProviderError):
    """Raised when authentication fails."""

    pass


class ModelNotFoundError(LLMProviderError):
    """Raised when the requested model doesn't exist."""

    pass


class ContextLengthError(LLMProviderError):
    """Raised when exceeding model context limits."""

    pass


class InvalidRequestError(LLMProviderError):
    """Raised when the request format is invalid."""

    pass


class ProviderConnectionError(LLMProviderError):
    """Raised when unable to connect to the provider."""

    pass


class ProviderTimeoutError(LLMProviderError):
    """Raised when a provider request times out."""

    pass
