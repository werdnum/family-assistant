"""Tests for the scripting LLM API."""

from unittest.mock import AsyncMock, patch

import pytest

from family_assistant.llm import LLMOutput
from family_assistant.scripting.apis.llm import (
    DEFAULT_MODEL,
    llm_call_async,
    llm_call_json_async,
)
from family_assistant.scripting.config import ScriptConfig
from family_assistant.scripting.monty_engine import MontyEngine


@pytest.fixture
def mock_llm_client() -> AsyncMock:
    """Create a mock LLM client."""
    mock = AsyncMock()
    mock.generate_response = AsyncMock(
        return_value=LLMOutput(content="Test LLM response")
    )
    return mock


@pytest.mark.no_db
def test_default_model() -> None:
    """Default model should be gemini-3-flash-preview."""
    assert DEFAULT_MODEL == "gemini-3-flash-preview"


@pytest.mark.no_db
async def test_llm_call_async(mock_llm_client: AsyncMock) -> None:
    """Test basic llm_call_async."""
    with patch(
        "family_assistant.scripting.apis.llm.LLMClientFactory.create_client",
        return_value=mock_llm_client,
    ) as mock_factory:
        result = await llm_call_async("Hello")

        mock_factory.assert_called_once_with({"model": DEFAULT_MODEL})
        assert result == "Test LLM response"


@pytest.mark.no_db
async def test_llm_call_async_with_system(mock_llm_client: AsyncMock) -> None:
    """Test llm_call_async with system prompt."""
    with patch(
        "family_assistant.scripting.apis.llm.LLMClientFactory.create_client",
        return_value=mock_llm_client,
    ):
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
    with patch(
        "family_assistant.scripting.apis.llm.LLMClientFactory.create_client",
        return_value=mock_llm_client,
    ) as mock_factory:
        await llm_call_async("Hello", model="gpt-4o")

        mock_factory.assert_called_once_with({"model": "gpt-4o"})


@pytest.mark.no_db
async def test_llm_call_json_async(mock_llm_client: AsyncMock) -> None:
    """Test llm_call_json_async returns parsed JSON."""
    mock_llm_client.generate_response = AsyncMock(
        return_value=LLMOutput(content='{"name": "Alice", "age": 30}')
    )

    with patch(
        "family_assistant.scripting.apis.llm.LLMClientFactory.create_client",
        return_value=mock_llm_client,
    ):
        result = await llm_call_json_async("Extract info")

        assert result == {"name": "Alice", "age": 30}


@pytest.mark.no_db
async def test_llm_call_json_async_with_schema(mock_llm_client: AsyncMock) -> None:
    """Test llm_call_json_async with schema parameter."""
    mock_llm_client.generate_response = AsyncMock(
        return_value=LLMOutput(content='{"title": "Test"}')
    )

    schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}},
    }

    with patch(
        "family_assistant.scripting.apis.llm.LLMClientFactory.create_client",
        return_value=mock_llm_client,
    ):
        result = await llm_call_json_async("Extract info", schema=schema)

        assert result == {"title": "Test"}

        call_args = mock_llm_client.generate_response.call_args
        messages = call_args[0][0]
        system_content = messages[0].content
        assert "JSON" in system_content
        assert "schema" in system_content


@pytest.mark.no_db
async def test_llm_call_json_strips_markdown(mock_llm_client: AsyncMock) -> None:
    """Test that llm_call_json_async strips markdown code fences."""
    mock_llm_client.generate_response = AsyncMock(
        return_value=LLMOutput(content='```json\n{"key": "value"}\n```')
    )

    with patch(
        "family_assistant.scripting.apis.llm.LLMClientFactory.create_client",
        return_value=mock_llm_client,
    ):
        result = await llm_call_json_async("Extract info")

        assert result == {"key": "value"}


@pytest.mark.no_db
async def test_llm_call_async_no_content(mock_llm_client: AsyncMock) -> None:
    """Test that llm_call_async raises on empty LLM response."""
    mock_llm_client.generate_response = AsyncMock(return_value=LLMOutput(content=None))

    with (
        patch(
            "family_assistant.scripting.apis.llm.LLMClientFactory.create_client",
            return_value=mock_llm_client,
        ),
        pytest.raises(ValueError, match="LLM returned no content"),
    ):
        await llm_call_async("Hello")


@pytest.mark.no_db
async def test_llm_available_in_engine(mock_llm_client: AsyncMock) -> None:
    """Test that llm() is available in MontyEngine scripts."""
    with patch(
        "family_assistant.scripting.apis.llm.LLMClientFactory.create_client",
        return_value=mock_llm_client,
    ):
        engine = MontyEngine()
        result = await engine.evaluate_async("llm('Summarise this')")
        assert result == "Test LLM response"


@pytest.mark.no_db
async def test_llm_json_available_in_engine(mock_llm_client: AsyncMock) -> None:
    """Test that llm_json() works from MontyEngine scripts."""
    mock_llm_client.generate_response = AsyncMock(
        return_value=LLMOutput(content='{"summary": "short"}')
    )

    with patch(
        "family_assistant.scripting.apis.llm.LLMClientFactory.create_client",
        return_value=mock_llm_client,
    ):
        engine = MontyEngine()
        result = await engine.evaluate_async("llm_json('Extract info')")
        assert result == {"summary": "short"}


@pytest.mark.no_db
async def test_llm_not_available_when_apis_disabled(mock_llm_client: AsyncMock) -> None:
    """Test that llm() is not available when APIs are disabled."""
    with patch(
        "family_assistant.scripting.apis.llm.LLMClientFactory.create_client",
        return_value=mock_llm_client,
    ):
        engine = MontyEngine(config=ScriptConfig(disable_apis=True))
        with pytest.raises(Exception, match="llm"):
            await engine.evaluate_async("llm('test')")
