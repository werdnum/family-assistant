"""Unit tests for RetryingLLMClient."""

# pylint: disable=no-name-in-module

from collections import Counter
from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, TypeVar, cast

import pytest
from pydantic import BaseModel

from family_assistant.llm import (
    JsonObject,
    LLMMessage,
    LLMOutput,
    LLMStreamEvent,
    UserMessageDict,
)
from family_assistant.llm.base import (
    InvalidRequestError,
    ProviderConnectionError,
    ProviderTimeoutError,
    RateLimitError,
    ServiceUnavailableError,
)
from family_assistant.llm.messages import UserMessage
from family_assistant.llm.retrying_client import RetryingLLMClient
from tests.factories.messages import (
    create_user_message,  # pylint: disable=no-name-in-module
)

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolAttachment, ToolDefinition

T = TypeVar("T", bound=BaseModel)
R = TypeVar("R")


class StructuredResult(BaseModel):
    answer: str


class ScriptedLLMClient:
    """LLM client that plays back scripted outcomes and counts each call.

    Each scripted outcome is either returned or, if it is an exception, raised.
    A call beyond the script fails, so an unexpected extra attempt is visible.
    """

    def __init__(
        self,
        *,
        responses: Sequence[LLMOutput | Exception] = (),
        structured: Sequence[BaseModel | Exception] = (),
        json_objects: Sequence[JsonObject | Exception] = (),
        formatted_message: UserMessageDict | None = None,
    ) -> None:
        self._responses = list(responses)
        self._structured = list(structured)
        self._json_objects = list(json_objects)
        self._formatted_message = formatted_message
        self.calls: Counter[str] = Counter()

    @staticmethod
    def _next(method: str, script: list[R | Exception]) -> R:
        if not script:
            raise AssertionError(f"Unscripted call to {method}")
        outcome = script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        tools: "list[ToolDefinition] | None" = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        self.calls["generate_response"] += 1
        return self._next("generate_response", self._responses)

    def generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        tools: "list[ToolDefinition] | None" = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        raise AssertionError("Streaming is not scripted in these tests")

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
    ) -> UserMessageDict:
        self.calls["format_user_message_with_file"] += 1
        if self._formatted_message is None:
            raise AssertionError("Unscripted call to format_user_message_with_file")
        return self._formatted_message

    def create_attachment_injection(
        self,
        attachment: "ToolAttachment",
    ) -> UserMessage:
        raise AssertionError("Attachment injection is not scripted in these tests")

    async def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
        max_retries: int = 2,
    ) -> T:
        self.calls["generate_structured"] += 1
        result = self._next("generate_structured", self._structured)
        assert isinstance(result, response_model)
        return cast("T", result)

    async def generate_json(
        self,
        messages: Sequence[LLMMessage],
        max_retries: int = 2,
    ) -> JsonObject:
        self.calls["generate_json"] += 1
        return self._next("generate_json", self._json_objects)


class AsyncClosingLLMClient(ScriptedLLMClient):
    """Client whose close is a coroutine."""

    def __init__(self) -> None:
        super().__init__()
        self.close_count = 0

    async def close(self) -> None:
        self.close_count += 1


class SyncClosingLLMClient(ScriptedLLMClient):
    """Client whose close is a plain method."""

    def __init__(self) -> None:
        super().__init__()
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


@pytest.mark.no_db
async def test_close_releases_async_and_sync_retry_clients() -> None:
    primary = AsyncClosingLLMClient()
    fallback = SyncClosingLLMClient()
    client = RetryingLLMClient(
        primary_client=primary,
        primary_model="primary",
        fallback_client=fallback,
        fallback_model="fallback",
    )

    await client.close()

    assert primary.close_count == 1
    assert fallback.close_count == 1


@pytest.mark.no_db
async def test_successful_primary_call() -> None:
    """Test successful call to primary client."""
    primary = ScriptedLLMClient(responses=[LLMOutput(content="Primary success")])
    fallback = ScriptedLLMClient()

    client = RetryingLLMClient(
        primary_client=primary,
        primary_model="test-model",
        fallback_client=fallback,
        fallback_model="fallback-model",
    )

    messages = [create_user_message("test")]
    response = await client.generate_response(messages)

    assert response.content == "Primary success"
    assert primary.calls["generate_response"] == 1
    assert fallback.calls["generate_response"] == 0


@pytest.mark.no_db
async def test_retry_on_connection_error() -> None:
    """Test retry on connection error."""
    primary = ScriptedLLMClient(
        responses=[
            ProviderConnectionError("Connection failed", "test", "test-model"),
            LLMOutput(content="Success after retry"),
        ]
    )
    fallback = ScriptedLLMClient()

    client = RetryingLLMClient(
        primary_client=primary,
        primary_model="test-model",
        fallback_client=fallback,
        fallback_model="fallback-model",
    )

    messages = [create_user_message("test")]
    response = await client.generate_response(messages)

    assert response.content == "Success after retry"
    assert primary.calls["generate_response"] == 2
    assert fallback.calls["generate_response"] == 0


@pytest.mark.no_db
async def test_retry_on_timeout_error() -> None:
    """Test retry on timeout error."""
    primary = ScriptedLLMClient(
        responses=[
            ProviderTimeoutError("Timeout", "test", "test-model"),
            LLMOutput(content="Success after timeout"),
        ]
    )

    client = RetryingLLMClient(
        primary_client=primary,
        primary_model="test-model",
        fallback_client=None,  # No fallback
    )

    messages = [create_user_message("test")]
    response = await client.generate_response(messages)

    assert response.content == "Success after timeout"
    assert primary.calls["generate_response"] == 2


@pytest.mark.no_db
async def test_retry_on_rate_limit() -> None:
    """Test retry on rate limit error."""
    primary = ScriptedLLMClient(
        responses=[
            RateLimitError("Rate limited", "test", "test-model"),
            LLMOutput(content="Success after rate limit"),
        ]
    )

    client = RetryingLLMClient(
        primary_client=primary,
        primary_model="test-model",
    )

    messages = [create_user_message("test")]
    response = await client.generate_response(messages)

    assert response.content == "Success after rate limit"
    assert primary.calls["generate_response"] == 2


@pytest.mark.no_db
async def test_retry_on_service_unavailable() -> None:
    """Test retry on service unavailable error."""
    primary = ScriptedLLMClient(
        responses=[
            ServiceUnavailableError("Service unavailable", "test", "test-model"),
            LLMOutput(content="Success after 503"),
        ]
    )

    client = RetryingLLMClient(
        primary_client=primary,
        primary_model="test-model",
    )

    messages = [create_user_message("test")]
    response = await client.generate_response(messages)

    assert response.content == "Success after 503"
    assert primary.calls["generate_response"] == 2


@pytest.mark.no_db
async def test_fallback_after_retry_failures() -> None:
    """Test fallback after primary retries fail."""
    primary = ScriptedLLMClient(
        responses=[
            RateLimitError("Rate limited", "test", "test-model"),
            RateLimitError("Still rate limited", "test", "test-model"),
        ]
    )
    fallback = ScriptedLLMClient(responses=[LLMOutput(content="Fallback success")])

    client = RetryingLLMClient(
        primary_client=primary,
        primary_model="test-model",
        fallback_client=fallback,
        fallback_model="fallback-model",
    )

    messages = [create_user_message("test")]
    response = await client.generate_response(messages)

    assert response.content == "Fallback success"
    assert primary.calls["generate_response"] == 2
    assert fallback.calls["generate_response"] == 1


@pytest.mark.no_db
async def test_non_retriable_error_goes_to_fallback() -> None:
    """Test non-retriable errors skip retry and go to fallback."""
    primary = ScriptedLLMClient(
        responses=[InvalidRequestError("Invalid request", "test", "test-model")]
    )
    fallback = ScriptedLLMClient(responses=[LLMOutput(content="Fallback handled it")])

    client = RetryingLLMClient(
        primary_client=primary,
        primary_model="test-model",
        fallback_client=fallback,
        fallback_model="fallback-model",
    )

    messages = [create_user_message("test")]
    response = await client.generate_response(messages)

    assert response.content == "Fallback handled it"
    # Should only try primary once (no retry for non-retriable)
    assert primary.calls["generate_response"] == 1
    assert fallback.calls["generate_response"] == 1


@pytest.mark.no_db
async def test_all_attempts_fail() -> None:
    """Test exception raised when all attempts fail."""
    primary_error = RateLimitError("Rate limited", "test", "test-model")
    primary = ScriptedLLMClient(responses=[primary_error, primary_error])
    fallback = ScriptedLLMClient(
        responses=[
            ServiceUnavailableError("Service down", "fallback", "fallback-model")
        ]
    )

    client = RetryingLLMClient(
        primary_client=primary,
        primary_model="test-model",
        fallback_client=fallback,
        fallback_model="fallback-model",
    )

    messages = [create_user_message("test")]

    with pytest.raises(RateLimitError) as exc_info:
        await client.generate_response(messages)

    # Should raise the primary error, not the fallback error
    assert exc_info.value is primary_error
    assert primary.calls["generate_response"] == 2
    assert fallback.calls["generate_response"] == 1


@pytest.mark.no_db
async def test_no_fallback_configured() -> None:
    """Test behavior when no fallback is configured."""
    error = RateLimitError("Rate limited", "test", "test-model")
    primary = ScriptedLLMClient(responses=[error, error])

    client = RetryingLLMClient(
        primary_client=primary,
        primary_model="test-model",
        fallback_client=None,  # No fallback
    )

    messages = [create_user_message("test")]

    with pytest.raises(RateLimitError):
        await client.generate_response(messages)

    # Should try twice (original + retry)
    assert primary.calls["generate_response"] == 2


@pytest.mark.no_db
async def test_same_model_fallback_skipped() -> None:
    """Test that fallback is skipped if it's the same model as primary."""
    error = RateLimitError("Rate limited", "test", "test-model")
    primary = ScriptedLLMClient(responses=[error, error])

    client = RetryingLLMClient(
        primary_client=primary,
        primary_model="test-model",
        fallback_client=primary,  # Same client
        fallback_model="test-model",  # Same model
    )

    messages = [create_user_message("test")]

    with pytest.raises(RateLimitError):
        await client.generate_response(messages)

    # Should only try primary twice, no fallback attempt
    assert primary.calls["generate_response"] == 2


@pytest.mark.no_db
async def test_format_user_message_with_file() -> None:
    """Test that format_user_message_with_file delegates to primary."""
    expected_result = UserMessageDict(role="user", content="file content")
    primary = ScriptedLLMClient(formatted_message=expected_result)
    fallback = ScriptedLLMClient(
        formatted_message=UserMessageDict(role="user", content="fallback content")
    )

    client = RetryingLLMClient(
        primary_client=primary,
        primary_model="test-model",
        fallback_client=fallback,
        fallback_model="fallback-model",
    )

    result = await client.format_user_message_with_file(
        "prompt", "/path/to/file", "text/plain", 1000
    )

    assert result == expected_result
    assert primary.calls["format_user_message_with_file"] == 1
    assert fallback.calls["format_user_message_with_file"] == 0


@pytest.mark.no_db
async def test_generate_structured_falls_back_after_primary_error() -> None:
    """Structured output should use fallback after a primary failure."""
    primary = ScriptedLLMClient(
        structured=[InvalidRequestError("invalid", "test", "test-model")]
    )
    fallback = ScriptedLLMClient(structured=[StructuredResult(answer="fallback")])

    client = RetryingLLMClient(
        primary_client=primary,
        primary_model="test-model",
        fallback_client=fallback,
        fallback_model="fallback-model",
    )

    result = await client.generate_structured(
        messages=[create_user_message("test")],
        response_model=StructuredResult,
    )

    assert result == StructuredResult(answer="fallback")
    assert primary.calls["generate_structured"] == 1
    assert fallback.calls["generate_structured"] == 1


@pytest.mark.no_db
async def test_generate_json_retries_primary_before_fallback() -> None:
    """JSON output should retry retriable errors before using fallback."""
    primary = ScriptedLLMClient(
        json_objects=[
            ProviderConnectionError("down", "test", "test-model"),
            ProviderConnectionError("still down", "test", "test-model"),
        ]
    )
    fallback = ScriptedLLMClient(json_objects=[{"answer": "fallback"}])

    client = RetryingLLMClient(
        primary_client=primary,
        primary_model="test-model",
        fallback_client=fallback,
        fallback_model="fallback-model",
    )

    result = await client.generate_json(messages=[create_user_message("test")])

    assert result == {"answer": "fallback"}
    assert primary.calls["generate_json"] == 2
    assert fallback.calls["generate_json"] == 1
