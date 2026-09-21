"""Unit tests for provider-native structured and JSON output modes."""

# pylint: disable=no-name-in-module

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel, ConfigDict

from family_assistant.llm import UserMessage
from family_assistant.llm.providers.anthropic_client import AnthropicClient
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.llm.providers.openai_client import OpenAIClient


class SampleResponse(BaseModel):
    answer: str


class StrictResponse(BaseModel):
    """Model whose JSON schema carries the extras guard and a mapping field."""

    model_config = ConfigDict(extra="forbid")

    answer: str
    values: dict[str, str]


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_openai_generate_structured_uses_native_parse() -> None:
    """OpenAI structured output should use the native parse endpoint."""
    client = OpenAIClient(api_key="test", model="gpt-4.1-nano")
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message = MagicMock(
        parsed=SampleResponse(answer="ok"),
        content='{"answer":"ok"}',
    )

    with patch.object(
        client.client.beta.chat.completions, "parse", new_callable=AsyncMock
    ) as mock_parse:
        mock_parse.return_value = response

        result = await client.generate_structured(
            messages=[UserMessage(content="Return structured output")],
            response_model=SampleResponse,
        )

    assert result == SampleResponse(answer="ok")
    assert mock_parse.await_args is not None
    assert mock_parse.await_args.kwargs["response_format"] is SampleResponse


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_openai_generate_json_uses_native_json_mode() -> None:
    """OpenAI JSON output should use response_format=json_object."""
    client = OpenAIClient(api_key="test", model="gpt-4.1-nano")
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message = MagicMock(content='{"answer":"ok"}')

    with patch.object(
        client.client.chat.completions, "create", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = response

        result = await client.generate_json(
            messages=[UserMessage(content="Return JSON")]
        )

    assert result == {"answer": "ok"}
    assert mock_create.await_args is not None
    assert mock_create.await_args.kwargs["response_format"] == {"type": "json_object"}


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_anthropic_generate_structured_uses_forced_tool_schema() -> None:
    """Anthropic structured output should use forced native tool use."""
    client = AnthropicClient(api_key="test", model="claude-sonnet-4-5")
    response = MagicMock()
    response.content = [
        SimpleNamespace(
            type="tool_use",
            name="return_structured_response",
            input={"answer": "ok"},
        )
    ]

    with patch.object(
        client.client.messages, "create", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = response

        result = await client.generate_structured(
            messages=[UserMessage(content="Return structured output")],
            response_model=SampleResponse,
        )

    assert result == SampleResponse(answer="ok")
    assert mock_create.await_args is not None
    tools = mock_create.await_args.kwargs["tools"]
    assert tools[0]["name"] == "return_structured_response"
    assert mock_create.await_args.kwargs["tool_choice"] == {
        "type": "tool",
        "name": "return_structured_response",
    }


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_anthropic_generate_json_uses_forced_object_tool() -> None:
    """Anthropic JSON output should use forced native tool use."""
    client = AnthropicClient(api_key="test", model="claude-sonnet-4-5")
    response = MagicMock()
    response.content = [
        SimpleNamespace(
            type="tool_use",
            name="return_json_object",
            input={"answer": "ok"},
        )
    ]

    with patch.object(
        client.client.messages, "create", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = response

        result = await client.generate_json(
            messages=[UserMessage(content="Return JSON")]
        )

    assert result == {"answer": "ok"}
    assert mock_create.await_args is not None
    tools = mock_create.await_args.kwargs["tools"]
    assert tools[0]["input_schema"]["type"] == "object"
    assert tools[0]["input_schema"]["additionalProperties"] is True


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_google_generate_structured_uses_response_schema() -> None:
    """Gemini structured output should use response_schema and JSON MIME type."""
    client = GoogleGenAIClient(api_key="test", model="gemini-3.8-flash")
    response = MagicMock()
    response.text = '{"answer":"ok"}'

    with patch.object(
        client.client.aio.models, "generate_content", new_callable=AsyncMock
    ) as mock_generate:
        mock_generate.return_value = response

        result = await client.generate_structured(
            messages=[UserMessage(content="Return structured output")],
            response_model=SampleResponse,
        )

    assert result == SampleResponse(answer="ok")
    assert mock_generate.await_args is not None
    config = mock_generate.await_args.kwargs["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == SampleResponse.model_json_schema()
    assert config.response_schema is None


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_google_generate_structured_sends_strict_schema_unaltered() -> None:
    """A model Gemini's OpenAPI subset would reject must reach the API intact.

    ``extra="forbid"`` and mapping fields both emit ``additionalProperties``, which
    the ``response_schema`` proto refuses outright. The JSON Schema path accepts it,
    so the schema is passed through rather than sanitized.
    """
    client = GoogleGenAIClient(api_key="test", model="gemini-3.8-flash")
    response = MagicMock()
    response.text = '{"answer":"ok","values":{"a":"b"}}'

    with patch.object(
        client.client.aio.models, "generate_content", new_callable=AsyncMock
    ) as mock_generate:
        mock_generate.return_value = response

        result = await client.generate_structured(
            messages=[UserMessage(content="Return structured output")],
            response_model=StrictResponse,
        )

    assert result == StrictResponse(answer="ok", values={"a": "b"})
    assert mock_generate.await_args is not None
    config = mock_generate.await_args.kwargs["config"]
    assert config.response_json_schema == StrictResponse.model_json_schema()
    assert "additionalProperties" in json.dumps(config.response_json_schema)


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_google_generate_structured_retry_adds_feedback() -> None:
    """Gemini structured retries should mutate the conversation with feedback."""
    client = GoogleGenAIClient(api_key="test", model="gemini-3.8-flash")
    invalid_response = MagicMock()
    invalid_response.text = "not-json"
    valid_response = MagicMock()
    valid_response.text = '{"answer":"ok"}'

    with patch.object(
        client.client.aio.models, "generate_content", new_callable=AsyncMock
    ) as mock_generate:
        mock_generate.side_effect = [invalid_response, valid_response]

        result = await client.generate_structured(
            messages=[UserMessage(content="Return structured output")],
            response_model=SampleResponse,
            max_retries=1,
        )

    assert result == SampleResponse(answer="ok")
    first_contents = mock_generate.await_args_list[0].kwargs["contents"]
    second_contents = mock_generate.await_args_list[1].kwargs["contents"]
    assert len(second_contents) > len(first_contents)


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_google_generate_json_uses_object_schema() -> None:
    """Gemini JSON output should use an object response schema."""
    client = GoogleGenAIClient(api_key="test", model="gemini-3.8-flash")
    response = MagicMock()
    response.text = '{"answer":"ok"}'

    with patch.object(
        client.client.aio.models, "generate_content", new_callable=AsyncMock
    ) as mock_generate:
        mock_generate.return_value = response

        result = await client.generate_json(
            messages=[UserMessage(content="Return JSON")]
        )

    assert result == {"answer": "ok"}
    assert mock_generate.await_args is not None
    config = mock_generate.await_args.kwargs["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_schema is None


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_google_generate_json_retry_adds_feedback() -> None:
    """Gemini JSON retries should mutate the conversation with feedback."""
    client = GoogleGenAIClient(api_key="test", model="gemini-3.8-flash")
    invalid_response = MagicMock()
    invalid_response.text = '["wrong"]'
    valid_response = MagicMock()
    valid_response.text = '{"answer":"ok"}'

    with patch.object(
        client.client.aio.models, "generate_content", new_callable=AsyncMock
    ) as mock_generate:
        mock_generate.side_effect = [invalid_response, valid_response]

        result = await client.generate_json(
            messages=[UserMessage(content="Return JSON")],
            max_retries=1,
        )

    assert result == {"answer": "ok"}
    first_contents = mock_generate.await_args_list[0].kwargs["contents"]
    second_contents = mock_generate.await_args_list[1].kwargs["contents"]
    assert len(second_contents) > len(first_contents)
