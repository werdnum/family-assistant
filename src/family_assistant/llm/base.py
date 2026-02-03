"""
Base exception hierarchy for LLM providers.

The data classes and protocol are defined in __init__.py to avoid circular imports.
"""


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


class ServiceUnavailableError(LLMProviderError):
    """Raised when the service is temporarily unavailable (503 errors)."""

    pass


class StructuredOutputError(LLMProviderError):
    """Raised when structured output generation fails after retries.

    This error indicates that the LLM was unable to generate a valid response
    matching the requested Pydantic model schema, even after retry attempts.
    """

    def __init__(
        self,
        message: str,
        provider: str,
        model: str,
        raw_response: str | None = None,
        validation_error: Exception | None = None,
    ) -> None:
        super().__init__(message, provider, model)
        self.raw_response = raw_response
        self.validation_error = validation_error
