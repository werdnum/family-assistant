"""Unit tests for schemaless JSON output and playback compatibility."""

# pylint: disable=no-name-in-module

import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from family_assistant.llm import (
    BaseLLMClient,
    LiteLLMClient,
    LLMMessage,
    LLMOutput,
    LLMStreamEvent,
    PlaybackLLMClient,
    StructuredOutputError,
    UserMessageDict,
)
from family_assistant.llm.messages import message_to_json_dict
from family_assistant.tools.types import ToolDefinition
from tests.factories.messages import (
    create_user_message,  # pylint: disable=no-name-in-module
)


class _JSONResponseClient(BaseLLMClient):
    """Small test double for BaseLLMClient JSON generation."""

    def __init__(self, responses: Sequence[str | None]) -> None:
        self.model = "test-provider/test-model"
        self._responses = list(responses)
        self.recorded_messages: list[list[LLMMessage]] = []

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        del tools, tool_choice
        self.recorded_messages.append(list(messages))
        if not self._responses:
            raise AssertionError("No more stubbed responses available")
        return LLMOutput(content=self._responses.pop(0))

    def generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        del messages, tools, tool_choice
        raise NotImplementedError

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
    ) -> UserMessageDict:
        del prompt_text, file_path, mime_type, max_text_length
        raise NotImplementedError


class _SimpleResponse(BaseModel):
    answer: str


@pytest.mark.no_db
class TestBaseLLMClientGenerateJSON:
    """Tests for the schemaless JSON-object API."""

    @pytest.mark.asyncio
    async def test_generate_json_retries_until_json_object(self) -> None:
        """Non-object JSON should trigger retry feedback and then succeed."""
        client = _JSONResponseClient(['["wrong-shape"]', '{"answer": "ok"}'])

        result = await client.generate_json(
            messages=[create_user_message("Return JSON")]
        )

        assert result == {"answer": "ok"}
        assert len(client.recorded_messages) == 2
        first_content = client.recorded_messages[0][0].content
        retry_feedback = client.recorded_messages[1][-1].content
        assert isinstance(first_content, str)
        assert isinstance(retry_feedback, str)
        assert "valid JSON object" in first_content
        assert "not a JSON object" in retry_feedback

    @pytest.mark.asyncio
    async def test_generate_json_raises_after_exhausting_retries(self) -> None:
        """Invalid JSON should surface as StructuredOutputError."""
        client = _JSONResponseClient(["not-json", "still-not-json"])

        with pytest.raises(StructuredOutputError) as exc_info:
            await client.generate_json(
                messages=[create_user_message("Return JSON")],
                max_retries=1,
            )

        assert "Failed to generate valid JSON output" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_litellm_generate_json_uses_native_json_mode(self) -> None:
        """LiteLLM generate_json should request provider-native JSON-object mode."""
        client = LiteLLMClient(model="openai/gpt-4.1-nano")
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message = MagicMock(content='{"answer": "ok"}')
        response.usage = None

        with patch(
            "family_assistant.llm.acompletion", new_callable=AsyncMock
        ) as mock_acompletion:
            mock_acompletion.return_value = response

            result = await client.generate_json(
                messages=[create_user_message("Return JSON")]
            )

        assert result == {"answer": "ok"}
        assert mock_acompletion.await_args is not None
        assert mock_acompletion.await_args.kwargs["response_format"] == {
            "type": "json_object"
        }


@pytest.mark.no_db
class TestPlaybackLLMClientCompatibility:
    """Tests for playback matching against legacy recordings."""

    @pytest.mark.asyncio
    async def test_generate_structured_matches_recording_without_max_retries(
        self,
        tmp_path: Path,
    ) -> None:
        """Legacy structured recordings without max_retries should still match."""
        recording_path = tmp_path / "structured.jsonl"
        messages = [create_user_message("Return structured output")]
        record = {
            "input": {
                "method": "generate_structured",
                "messages": [message_to_json_dict(message) for message in messages],
                "response_model_name": "_SimpleResponse",
                "response_model_schema": _SimpleResponse.model_json_schema(),
            },
            "output": {
                "model_name": "_SimpleResponse",
                "model_data": {"answer": "ok"},
            },
        }
        recording_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

        client = PlaybackLLMClient(str(recording_path))

        result = await client.generate_structured(
            messages=messages,
            response_model=_SimpleResponse,
        )

        assert result == _SimpleResponse(answer="ok")

    @pytest.mark.asyncio
    async def test_generate_json_matches_recording_without_max_retries(
        self,
        tmp_path: Path,
    ) -> None:
        """Legacy JSON recordings without max_retries should still match."""
        recording_path = tmp_path / "json.jsonl"
        messages = [create_user_message("Return JSON")]
        record = {
            "input": {
                "method": "generate_json",
                "messages": [message_to_json_dict(message) for message in messages],
            },
            "output": {
                "json_data": {"answer": "ok"},
            },
        }
        recording_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

        client = PlaybackLLMClient(str(recording_path))

        result = await client.generate_json(messages=messages)

        assert result == {"answer": "ok"}
