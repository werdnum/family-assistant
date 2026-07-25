"""Token-usage instrumentation for the Google and OpenAI providers.

Streaming is the path the processing loop actually uses, so usage that is only
reported for non-streaming calls leaves the default profiles unmeasurable.
"""

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest

from family_assistant.llm import LLMStreamEvent
from family_assistant.llm.messages import UserMessage
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.llm.providers.openai_client import OpenAIClient


@pytest.mark.no_db
class TestGeminiUsageMetadata:
    @staticmethod
    def _usage(**kwargs: int | None) -> SimpleNamespace:
        return SimpleNamespace(
            prompt_token_count=kwargs.get("prompt_token_count", 0),
            candidates_token_count=kwargs.get("candidates_token_count", 0),
            total_token_count=kwargs.get("total_token_count", 0),
            cached_content_token_count=kwargs.get("cached_content_token_count"),
            thoughts_token_count=kwargs.get("thoughts_token_count"),
        )

    def test_cache_hits_are_reported_without_inflating_the_total(self) -> None:
        """Gemini's cached count is a subset of the prompt, unlike Anthropic's."""
        info = GoogleGenAIClient._reasoning_info_from_usage_metadata(
            self._usage(
                prompt_token_count=5000,
                candidates_token_count=100,
                total_token_count=5100,
                cached_content_token_count=4000,
            )
        )

        assert info.get("cached_prompt_tokens") == 4000
        assert info.get("prompt_tokens") == 5000
        assert info.get("total_tokens") == 5100

    def test_thinking_tokens_are_reported(self) -> None:
        info = GoogleGenAIClient._reasoning_info_from_usage_metadata(
            self._usage(prompt_token_count=10, thoughts_token_count=250)
        )

        assert info.get("reasoning_tokens") == 250

    def test_absent_optional_counts_are_omitted(self) -> None:
        info = GoogleGenAIClient._reasoning_info_from_usage_metadata(
            self._usage(prompt_token_count=10, candidates_token_count=2)
        )

        assert "cached_prompt_tokens" not in info
        assert "reasoning_tokens" not in info

    def test_reported_zero_cache_hit_is_preserved(self) -> None:
        """A reported miss must stay distinguishable from nothing reported."""
        info = GoogleGenAIClient._reasoning_info_from_usage_metadata(
            self._usage(prompt_token_count=10, cached_content_token_count=0)
        )

        assert info.get("cached_prompt_tokens") == 0

    def test_none_counts_do_not_crash(self) -> None:
        """The SDK leaves counts unset rather than zero on some responses."""
        info = GoogleGenAIClient._reasoning_info_from_usage_metadata(
            SimpleNamespace(
                prompt_token_count=None,
                candidates_token_count=None,
                total_token_count=None,
            )
        )

        assert info.get("prompt_tokens") == 0
        assert info.get("total_tokens") == 0


@pytest.mark.no_db
class TestOpenAICachedPromptTokens:
    def test_reads_chat_completions_shape(self) -> None:
        usage = SimpleNamespace(
            prompt_tokens_details=SimpleNamespace(cached_tokens=1024)
        )

        assert OpenAIClient._cached_prompt_tokens(usage) == 1024

    def test_reads_responses_shape(self) -> None:
        usage = SimpleNamespace(input_tokens_details=SimpleNamespace(cached_tokens=512))

        assert OpenAIClient._cached_prompt_tokens(usage) == 512

    def test_reads_raw_dict_payloads(self) -> None:
        """Streaming replay and the Responses stream hand back plain dicts."""
        assert (
            OpenAIClient._cached_prompt_tokens({
                "prompt_tokens_details": {"cached_tokens": 256}
            })
            == 256
        )

    def test_absent_cache_field_is_none_not_zero(self) -> None:
        """None means "nothing reported"; 0 means a known miss."""
        assert OpenAIClient._cached_prompt_tokens(SimpleNamespace()) is None
        assert OpenAIClient._cached_prompt_tokens({}) is None

    def test_reported_zero_is_preserved(self) -> None:
        """A reported miss must not be dropped, or hit rates read too high."""
        usage = SimpleNamespace(prompt_tokens_details=SimpleNamespace(cached_tokens=0))

        assert OpenAIClient._cached_prompt_tokens(usage) == 0

    def test_responses_cache_write_tokens_are_read(self) -> None:
        """The Responses API reports cache writes even though the pinned SDK
        does not type the field, so it is only reachable by name."""
        usage = {
            "input_tokens_details": {"cache_write_tokens": 128, "cached_tokens": 0}
        }

        assert OpenAIClient._cache_write_tokens(usage) == 128
        assert OpenAIClient._cached_prompt_tokens(usage) == 0

    def test_chat_completions_reports_no_cache_write(self) -> None:
        usage = SimpleNamespace(prompt_tokens_details=SimpleNamespace(cached_tokens=64))

        assert OpenAIClient._cache_write_tokens(usage) is None


class _UsageChunk:
    """A final streaming chunk carrying usage and no choices, as OpenAI sends."""

    choices: ClassVar[list[object]] = []

    def __init__(self, cached_tokens: int) -> None:
        self.usage = SimpleNamespace(
            prompt_tokens=5000,
            completion_tokens=100,
            total_tokens=5100,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
        )


@pytest.mark.no_db
class TestOpenAIStreamUsage:
    @staticmethod
    async def _captured_params(
        client: OpenAIClient,
        chunks: list[object],
        # ast-grep-ignore: no-dict-any - captures arbitrary SDK request kwargs
    ) -> tuple[dict[str, Any], list[LLMStreamEvent]]:
        # ast-grep-ignore: no-dict-any - captures arbitrary SDK request kwargs
        captured: dict[str, Any] = {}

        async def _fake_create(**kwargs: Any) -> AsyncIterator[object]:  # noqa: ANN401
            captured.update(kwargs)

            async def _iter() -> AsyncIterator[object]:
                for chunk in chunks:
                    yield chunk

            return _iter()

        client.client = cast(
            "Any",
            SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=_fake_create))
            ),
        )
        events = [
            event
            async for event in client.generate_response_stream(
                messages=[UserMessage(content="hi")]
            )
        ]
        return captured, events

    async def test_direct_openai_requests_usage_on_the_stream(self) -> None:
        """Without stream_options the API sends no usage for a streamed turn."""
        client = OpenAIClient(api_key="test", model="gpt-5.5")

        params, _ = await self._captured_params(client, [])

        assert params["stream_options"] == {"include_usage": True}

    async def test_compatible_endpoint_is_not_sent_stream_options(self) -> None:
        """An endpoint that rejects unknown parameters must not be sent them."""
        client = OpenAIClient(
            api_key="test", model="gpt-5.5", base_url="https://openrouter.ai/api/v1"
        )

        params, _ = await self._captured_params(client, [])

        assert "stream_options" not in params

    async def test_model_parameters_can_override_the_default(self) -> None:
        """Operators on a compatible endpoint need a way to opt in."""
        client = OpenAIClient(
            api_key="test",
            model="gpt-5.5",
            model_parameters={"gpt-5.5": {"stream_options": {"include_usage": False}}},
        )

        params, _ = await self._captured_params(client, [])

        assert params["stream_options"] == {"include_usage": False}

    async def test_streaming_preserves_reasoning_tokens(self) -> None:
        """include_usage makes this reachable on a streamed turn for the first
        time; the non-streaming path already reported it."""
        client = OpenAIClient(api_key="test", model="gpt-5.5")
        chunk = _UsageChunk(cached_tokens=0)
        chunk.usage.completion_tokens_details = SimpleNamespace(reasoning_tokens=777)

        _, events = await self._captured_params(client, [chunk])

        done = [event for event in events if event.type == "done"]
        reasoning_info = (done[-1].metadata or {}).get("reasoning_info")
        assert reasoning_info is not None
        assert reasoning_info.get("reasoning_tokens") == 777

    async def test_usage_only_final_chunk_is_not_skipped(self) -> None:
        """The usage chunk has empty choices, so the content guards skip it."""
        client = OpenAIClient(api_key="test", model="gpt-5.5")

        _, events = await self._captured_params(
            client, [_UsageChunk(cached_tokens=4000)]
        )

        done = [event for event in events if event.type == "done"]
        assert done
        reasoning_info = (done[-1].metadata or {}).get("reasoning_info")
        assert reasoning_info is not None
        assert reasoning_info.get("cached_prompt_tokens") == 4000
        assert reasoning_info.get("prompt_tokens") == 5000


@pytest.mark.no_db
class TestOpenAIReplayStreamUsage:
    """VCR replay hands back parsed dicts, and had the same empty-choices bug."""

    async def _replay_events(
        self,
        # ast-grep-ignore: no-dict-any - replay chunks are parsed JSON
        chunk_dicts: list[dict[str, Any]],
    ) -> list[LLMStreamEvent]:
        client = OpenAIClient(api_key="test", model="gpt-5.5")
        return [
            event async for event in client._emit_events_from_chunk_dicts(chunk_dicts)
        ]

    async def test_usage_only_chunk_is_captured(self) -> None:
        events = await self._replay_events([
            {
                "choices": [
                    {"delta": {"content": "hi"}, "index": 0, "finish_reason": None}
                ]
            },
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 500,
                    "completion_tokens": 10,
                    "total_tokens": 510,
                    "prompt_tokens_details": {"cached_tokens": 384},
                },
            },
        ])

        done = [event for event in events if event.type == "done"]
        assert done
        reasoning_info = (done[-1].metadata or {}).get("reasoning_info")
        assert reasoning_info is not None, (
            "replayed stream emitted a done event with no usage at all"
        )
        assert reasoning_info.get("cached_prompt_tokens") == 384
        assert reasoning_info.get("prompt_tokens") == 500
