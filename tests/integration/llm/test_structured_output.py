"""Integration tests for structured output support in LLM clients."""

import os
from collections.abc import Awaitable, Callable

import pytest
import pytest_asyncio
from pydantic import BaseModel, Field

from family_assistant.llm import LLMInterface, StructuredOutputError
from family_assistant.llm.factory import LLMClientFactory
from tests.factories.messages import create_system_message, create_user_message
from tests.mocks.mock_llm import RuleBasedMockLLMClient

from .vcr_helpers import sanitize_response

# --- Test Models ---


class SimpleResponse(BaseModel):
    """Simple response model for basic tests."""

    answer: str
    confidence: float = Field(ge=0.0, le=1.0)


class PersonInfo(BaseModel):
    """Person information model."""

    name: str
    age: int = Field(ge=0, le=150)
    occupation: str | None = None


class NestedResponse(BaseModel):
    """Response with nested model."""

    person: PersonInfo
    summary: str


class MathResult(BaseModel):
    """Math calculation result."""

    expression: str
    result: int
    explanation: str


# --- Fixtures ---


@pytest_asyncio.fixture
async def llm_client_factory() -> Callable[
    [str, str, str | None], Awaitable[LLMInterface]
]:
    """Factory fixture for creating LLM clients."""

    async def _create_client(
        provider: str, model: str, api_key: str | None = None
    ) -> LLMInterface:
        """Create an LLM client for testing."""
        if api_key is None:
            if provider == "openai":
                api_key = os.getenv("OPENAI_API_KEY", "test-openai-key")
            elif provider == "google":
                api_key = os.getenv("GEMINI_API_KEY", "test-gemini-key")
            elif provider == "anthropic":
                api_key = os.getenv("ANTHROPIC_API_KEY", "test-anthropic-key")
            else:
                api_key = "test-api-key"

        config = {
            "provider": provider,
            "model": model,
            "api_key": api_key,
        }

        if provider == "google":
            config["api_base"] = "https://generativelanguage.googleapis.com/v1beta"

        return LLMClientFactory.create_client(config)

    return _create_client


# --- Mock Client Tests ---


@pytest.mark.no_db
class TestMockClientStructuredOutput:
    """Tests for structured output with the mock LLM client."""

    async def test_mock_client_returns_structured_response(self) -> None:
        """Test that mock client can return structured responses."""
        expected_response = SimpleResponse(answer="42", confidence=0.95)

        def always_match(_args: dict) -> bool:
            return True

        mock_client = RuleBasedMockLLMClient(
            rules=[],
            structured_rules=[(always_match, expected_response)],
        )

        messages = [create_user_message("What is the answer?")]

        result = await mock_client.generate_structured(
            messages=messages, response_model=SimpleResponse
        )

        assert isinstance(result, SimpleResponse)
        assert result.answer == "42"
        assert result.confidence == 0.95

    async def test_mock_client_callable_structured_rule(self) -> None:
        """Test that mock client supports callable structured rules."""

        def always_match(_args: dict) -> bool:
            return True

        def generate_response(args: dict) -> PersonInfo:
            # Dynamic response based on input
            messages = args.get("messages", [])
            if messages and "Alice" in str(messages[-1].content):
                return PersonInfo(name="Alice", age=30, occupation="Engineer")
            return PersonInfo(name="Unknown", age=0)

        mock_client = RuleBasedMockLLMClient(
            rules=[],
            structured_rules=[(always_match, generate_response)],
        )

        messages = [create_user_message("Tell me about Alice")]

        result = await mock_client.generate_structured(
            messages=messages, response_model=PersonInfo
        )

        assert isinstance(result, PersonInfo)
        assert result.name == "Alice"
        assert result.age == 30

    async def test_mock_client_no_matching_rule_raises_error(self) -> None:
        """Test that mock client raises StructuredOutputError when no rule matches."""

        def never_match(_args: dict) -> bool:
            return False

        mock_client = RuleBasedMockLLMClient(
            rules=[],
            structured_rules=[
                (never_match, SimpleResponse(answer="x", confidence=0.5))
            ],
        )

        messages = [create_user_message("What is the answer?")]

        with pytest.raises(StructuredOutputError) as exc_info:
            await mock_client.generate_structured(
                messages=messages, response_model=SimpleResponse
            )

        assert "No matching structured rule found" in str(exc_info.value)

    async def test_mock_client_records_structured_calls(self) -> None:
        """Test that mock client records generate_structured calls."""
        expected_response = SimpleResponse(answer="test", confidence=0.8)

        def always_match(_args: dict) -> bool:
            return True

        mock_client = RuleBasedMockLLMClient(
            rules=[],
            structured_rules=[(always_match, expected_response)],
        )

        messages = [create_user_message("Test message")]

        await mock_client.generate_structured(
            messages=messages, response_model=SimpleResponse
        )

        calls = mock_client.get_calls()
        assert len(calls) == 1
        assert calls[0]["method_name"] == "generate_structured"
        assert calls[0]["kwargs"]["response_model_name"] == "SimpleResponse"


# --- Provider Integration Tests ---


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.parametrize(
    "provider,model",
    [
        ("openai", "gpt-4.1-nano"),
        ("google", "gemini-3-flash-preview"),
        ("anthropic", "claude-haiku-4-5-20251001"),
    ],
)
async def test_basic_structured_output(
    provider: str,
    model: str,
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
) -> None:
    """Test basic structured output for each provider."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_factory(provider, model, None)

    messages = [
        create_user_message(
            "What is 2 + 2? Provide the expression, result, and a brief explanation."
        )
    ]

    result = await client.generate_structured(
        messages=messages, response_model=MathResult
    )

    assert isinstance(result, MathResult)
    assert result.result == 4
    assert "2" in result.expression
    assert len(result.explanation) > 0


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.parametrize(
    "provider,model",
    [
        ("openai", "gpt-4.1-nano"),
        ("google", "gemini-3-flash-preview"),
        ("anthropic", "claude-haiku-4-5-20251001"),
    ],
)
async def test_structured_output_with_system_message(
    provider: str,
    model: str,
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
) -> None:
    """Test structured output with system messages."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_factory(provider, model, None)

    messages = [
        create_system_message(
            "You are a helpful assistant that provides accurate information."
        ),
        create_user_message(
            "Create a person profile for John who is 25 years old and works as a software developer."
        ),
    ]

    result = await client.generate_structured(
        messages=messages, response_model=PersonInfo
    )

    assert isinstance(result, PersonInfo)
    assert "john" in result.name.lower()
    assert result.age == 25
    assert result.occupation is not None


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.parametrize(
    "provider,model",
    [
        ("openai", "gpt-4.1-nano"),
        ("google", "gemini-3-flash-preview"),
        ("anthropic", "claude-haiku-4-5-20251001"),
    ],
)
async def test_nested_structured_output(
    provider: str,
    model: str,
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
) -> None:
    """Test structured output with nested models."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_factory(provider, model, None)

    messages = [
        create_user_message(
            "Create a profile for Sarah, age 35, who works as a doctor. "
            "Also provide a one-sentence summary about her."
        ),
    ]

    result = await client.generate_structured(
        messages=messages, response_model=NestedResponse
    )

    assert isinstance(result, NestedResponse)
    assert isinstance(result.person, PersonInfo)
    assert "sarah" in result.person.name.lower()
    assert result.person.age == 35
    assert len(result.summary) > 0


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.parametrize(
    "provider,model",
    [
        ("openai", "gpt-4.1-nano"),
    ],
)
async def test_structured_output_with_optional_fields(
    provider: str,
    model: str,
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
) -> None:
    """Test structured output with optional fields."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_factory(provider, model, None)

    # Request without occupation to test optional field
    messages = [
        create_user_message(
            "Create a person profile for Bob who is 40 years old. Don't include an occupation."
        )
    ]

    result = await client.generate_structured(
        messages=messages, response_model=PersonInfo
    )

    assert isinstance(result, PersonInfo)
    assert "bob" in result.name.lower()
    assert result.age == 40
    # occupation can be None or any string value


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.parametrize(
    "provider,model",
    [
        ("openai", "gpt-4.1-nano"),
        ("google", "gemini-3-flash-preview"),
        ("anthropic", "claude-haiku-4-5-20251001"),
    ],
)
async def test_structured_output_with_constrained_fields(
    provider: str,
    model: str,
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
) -> None:
    """Test structured output respects field constraints."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_factory(provider, model, None)

    messages = [
        create_user_message(
            "What is the capital of France? Provide a one-word answer and your confidence level between 0 and 1."
        )
    ]

    result = await client.generate_structured(
        messages=messages, response_model=SimpleResponse
    )

    assert isinstance(result, SimpleResponse)
    assert "paris" in result.answer.lower()
    # Confidence should be within the constrained range
    assert 0.0 <= result.confidence <= 1.0
