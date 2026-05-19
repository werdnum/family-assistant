"""Tests for the scripting LLM API."""

from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from family_assistant.llm import LLMOutput
from family_assistant.llm.base import StructuredOutputError
from family_assistant.scripting.apis.llm import (
    DEFAULT_MODEL,
    llm_call_async,
    llm_call_json_async,
)
from family_assistant.scripting.config import ScriptConfig
from family_assistant.scripting.monty_engine import MontyEngine

# Patch location: llm_call_async now uses one_shot internally
PATCH_TARGET = "family_assistant.llm.one_shot.LLMClientFactory.create_client"


@pytest.fixture
def mock_llm_client() -> AsyncMock:
    """Create a mock LLM client."""
    mock = AsyncMock()
    mock.generate_response = AsyncMock(
        return_value=LLMOutput(content="Test LLM response")
    )
    mock.generate_json = AsyncMock(return_value={"key": "value"})
    return mock


@pytest.mark.no_db
def test_default_model() -> None:
    """Default model should be gemini-3.5-flash."""
    assert DEFAULT_MODEL == "gemini-3.5-flash"


@pytest.mark.no_db
async def test_llm_call_async(mock_llm_client: AsyncMock) -> None:
    """Test basic llm_call_async."""
    with patch(PATCH_TARGET, return_value=mock_llm_client) as mock_factory:
        result = await llm_call_async("Hello")

        mock_factory.assert_called_once_with({"model": DEFAULT_MODEL})
        assert result == "Test LLM response"


@pytest.mark.no_db
async def test_llm_call_async_with_system(mock_llm_client: AsyncMock) -> None:
    """Test llm_call_async with system prompt."""
    with patch(PATCH_TARGET, return_value=mock_llm_client):
        await llm_call_async("Hello", system="You are helpful.")

        call_args = mock_llm_client.generate_response.call_args
        messages = call_args[0][0]
        assert len(messages) == 2
        assert messages[0].role == "system"
        assert messages[0].content == "You are helpful."
        assert messages[1].role == "user"


@pytest.mark.no_db
async def test_llm_call_async_with_custom_model(mock_llm_client: AsyncMock) -> None:
    """Test llm_call_async with custom model."""
    with patch(PATCH_TARGET, return_value=mock_llm_client) as mock_factory:
        await llm_call_async("Hello", model="gpt-4o")

        mock_factory.assert_called_once_with({"model": "gpt-4o"})


@pytest.mark.no_db
async def test_llm_call_json_async(mock_llm_client: AsyncMock) -> None:
    """Test llm_call_json_async returns parsed JSON using native JSON mode."""
    mock_llm_client.generate_json = AsyncMock(return_value={"name": "Alice", "age": 30})

    with patch(PATCH_TARGET, return_value=mock_llm_client):
        result = await llm_call_json_async("Extract info")

        assert result == {"name": "Alice", "age": 30}
        mock_llm_client.generate_json.assert_called_once()


@pytest.mark.no_db
async def test_llm_call_json_async_with_schema(mock_llm_client: AsyncMock) -> None:
    """Test llm_call_json_async with schema parameter includes schema in system message."""
    mock_llm_client.generate_json = AsyncMock(return_value={"title": "Test"})

    schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}},
    }

    with patch(PATCH_TARGET, return_value=mock_llm_client):
        result = await llm_call_json_async("Extract info", schema=schema)

        assert result == {"title": "Test"}

        # Verify schema was included in system message
        call_args = mock_llm_client.generate_json.call_args
        messages = call_args[0][0]
        assert len(messages) == 2
        system_content = messages[0].content
        assert "schema" in system_content.lower()
        assert '"title"' in system_content


@pytest.mark.no_db
async def test_llm_call_json_async_with_system(mock_llm_client: AsyncMock) -> None:
    """Test llm_call_json_async with custom system message."""
    mock_llm_client.generate_json = AsyncMock(return_value={"result": "ok"})

    with patch(PATCH_TARGET, return_value=mock_llm_client):
        await llm_call_json_async("Extract info", system="Be concise.")

        call_args = mock_llm_client.generate_json.call_args
        messages = call_args[0][0]
        assert len(messages) == 2
        assert messages[0].role == "system"
        assert "Be concise." in messages[0].content


@pytest.mark.no_db
async def test_llm_call_json_async_error_handling(mock_llm_client: AsyncMock) -> None:
    """Test llm_call_json_async propagates StructuredOutputError."""
    mock_llm_client.generate_json = AsyncMock(
        side_effect=StructuredOutputError(
            message="Failed to parse JSON", provider="test", model="test-model"
        )
    )

    with (
        patch(PATCH_TARGET, return_value=mock_llm_client),
        pytest.raises(StructuredOutputError, match="Failed to parse JSON"),
    ):
        await llm_call_json_async("Extract info")


@pytest.mark.no_db
async def test_llm_call_async_no_content(mock_llm_client: AsyncMock) -> None:
    """Test that llm_call_async raises on empty LLM response."""
    mock_llm_client.generate_response = AsyncMock(return_value=LLMOutput(content=None))

    with (
        patch(PATCH_TARGET, return_value=mock_llm_client),
        pytest.raises(ValueError, match="LLM returned no content"),
    ):
        await llm_call_async("Hello")


@pytest.mark.no_db
async def test_llm_available_in_engine(mock_llm_client: AsyncMock) -> None:
    """Test that llm() is available in MontyEngine scripts."""
    with patch(PATCH_TARGET, return_value=mock_llm_client):
        engine = MontyEngine(default_timezone=ZoneInfo("Australia/Sydney"))
        result = await engine.evaluate_async("llm('Summarise this')")
        assert result == "Test LLM response"


@pytest.mark.no_db
async def test_llm_json_available_in_engine(mock_llm_client: AsyncMock) -> None:
    """Test that llm_json() works from MontyEngine scripts."""
    mock_llm_client.generate_json = AsyncMock(return_value={"summary": "short"})

    with patch(PATCH_TARGET, return_value=mock_llm_client):
        engine = MontyEngine(default_timezone=ZoneInfo("Australia/Sydney"))
        result = await engine.evaluate_async("llm_json('Extract info')")
        assert result == {"summary": "short"}


@pytest.mark.no_db
async def test_llm_not_available_when_llm_api_disabled(
    mock_llm_client: AsyncMock,
) -> None:
    """Test that llm() is not available when the LLM API is disabled."""
    with patch(PATCH_TARGET, return_value=mock_llm_client):
        engine = MontyEngine(
            config=ScriptConfig(enable_llm_api=False),
            default_timezone=ZoneInfo("Australia/Sydney"),
        )
        with pytest.raises(Exception, match="llm"):
            await engine.evaluate_async("llm('test')")
