"""Unit tests for one_shot LLM utilities."""

from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel

from family_assistant.llm import LLMOutput
from family_assistant.llm.one_shot import DEFAULT_MODEL, one_shot, one_shot_structured


class SummaryModel(BaseModel):
    """Model for structured output tests."""

    title: str
    key_points: list[str]


@pytest.fixture
def mock_llm_client() -> AsyncMock:
    """Create a mock LLM client."""
    mock = AsyncMock()
    mock.generate_response = AsyncMock(return_value=LLMOutput(content="Test response"))
    return mock


@pytest.mark.no_db
async def test_one_shot_simple(mock_llm_client: AsyncMock) -> None:
    """Test basic one_shot call."""
    with patch(
        "family_assistant.llm.one_shot.LLMClientFactory.create_client",
        return_value=mock_llm_client,
    ) as mock_factory:
        result = await one_shot("Hello, world!")

        mock_factory.assert_called_once_with({"model": DEFAULT_MODEL})
        mock_llm_client.generate_response.assert_called_once()

        call_args = mock_llm_client.generate_response.call_args
        messages = call_args[0][0]
        assert len(messages) == 1
        assert messages[0].role == "user"
        assert messages[0].content == "Hello, world!"

        assert result == "Test response"


@pytest.mark.no_db
async def test_one_shot_with_system_message(mock_llm_client: AsyncMock) -> None:
    """Test one_shot with system message."""
    with patch(
        "family_assistant.llm.one_shot.LLMClientFactory.create_client",
        return_value=mock_llm_client,
    ):
        await one_shot(
            "Hello",
            system="You are a helpful assistant.",
        )

        call_args = mock_llm_client.generate_response.call_args
        messages = call_args[0][0]
        assert len(messages) == 2
        assert messages[0].role == "system"
        assert messages[0].content == "You are a helpful assistant."
        assert messages[1].role == "user"
        assert messages[1].content == "Hello"


@pytest.mark.no_db
async def test_one_shot_with_custom_model(mock_llm_client: AsyncMock) -> None:
    """Test one_shot with custom model."""
    with patch(
        "family_assistant.llm.one_shot.LLMClientFactory.create_client",
        return_value=mock_llm_client,
    ) as mock_factory:
        await one_shot("Hello", model="gpt-4o")

        mock_factory.assert_called_once_with({"model": "gpt-4o"})


@pytest.mark.no_db
async def test_one_shot_raises_on_empty_content(mock_llm_client: AsyncMock) -> None:
    """Test one_shot raises ValueError when LLM returns no content."""
    mock_llm_client.generate_response = AsyncMock(return_value=LLMOutput(content=None))

    with (
        patch(
            "family_assistant.llm.one_shot.LLMClientFactory.create_client",
            return_value=mock_llm_client,
        ),
        pytest.raises(ValueError, match="LLM returned no content"),
    ):
        await one_shot("Hello")


@pytest.mark.no_db
async def test_one_shot_structured(mock_llm_client: AsyncMock) -> None:
    """Test one_shot_structured call."""
    expected_result = SummaryModel(title="Test", key_points=["Point 1", "Point 2"])
    mock_llm_client.generate_structured = AsyncMock(return_value=expected_result)

    with patch(
        "family_assistant.llm.one_shot.LLMClientFactory.create_client",
        return_value=mock_llm_client,
    ) as mock_factory:
        result = await one_shot_structured(
            "Summarize this",
            response_model=SummaryModel,
        )

        mock_factory.assert_called_once_with({"model": DEFAULT_MODEL})

        call_args = mock_llm_client.generate_structured.call_args
        messages = call_args[0][0]
        response_model = call_args[0][1]

        assert len(messages) == 1
        assert messages[0].role == "user"
        assert response_model is SummaryModel

        assert result == expected_result
        assert result.title == "Test"
        assert result.key_points == ["Point 1", "Point 2"]


@pytest.mark.no_db
async def test_one_shot_structured_with_system(mock_llm_client: AsyncMock) -> None:
    """Test one_shot_structured with system message."""
    expected_result = SummaryModel(title="Test", key_points=[])
    mock_llm_client.generate_structured = AsyncMock(return_value=expected_result)

    with patch(
        "family_assistant.llm.one_shot.LLMClientFactory.create_client",
        return_value=mock_llm_client,
    ):
        await one_shot_structured(
            "Summarize this",
            response_model=SummaryModel,
            system="You are a summarizer.",
        )

        call_args = mock_llm_client.generate_structured.call_args
        messages = call_args[0][0]

        assert len(messages) == 2
        assert messages[0].role == "system"
        assert messages[0].content == "You are a summarizer."
        assert messages[1].role == "user"


@pytest.mark.no_db
async def test_one_shot_structured_with_custom_model(
    mock_llm_client: AsyncMock,
) -> None:
    """Test one_shot_structured with custom model."""
    expected_result = SummaryModel(title="Test", key_points=[])
    mock_llm_client.generate_structured = AsyncMock(return_value=expected_result)

    with patch(
        "family_assistant.llm.one_shot.LLMClientFactory.create_client",
        return_value=mock_llm_client,
    ) as mock_factory:
        await one_shot_structured(
            "Summarize this",
            response_model=SummaryModel,
            model="claude-3-sonnet",
        )

        mock_factory.assert_called_once_with({"model": "claude-3-sonnet"})


@pytest.mark.no_db
def test_default_model_is_gemini_flash() -> None:
    """Test that the default model is gemini-3-flash-preview."""
    assert DEFAULT_MODEL == "gemini-3-flash-preview"
