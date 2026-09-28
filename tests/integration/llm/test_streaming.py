"""Integration tests for LLM streaming functionality with unified record/replay.

This file contains streaming tests for multiple LLM providers with a unified
record/replay interface:

- **OpenAI**: Uses VCR.py for HTTP-level recording
- **Google Gemini**: Uses SDK's built-in DebugConfig (native streaming support)

## Unified Record/Replay Interface

All tests use the `llm_replay_config` fixture which automatically selects the
appropriate mechanism based on the provider being tested.

### Environment Variables

**LLM_RECORD_MODE** - Controls recording behavior for all providers:
  - `replay` (default): Only use existing recordings (safe for CI, no API calls)
  - `auto`: Record if missing, else replay (convenient for development)
  - `record`: Force re-record everything (requires API keys)

### Usage Examples

```bash
# Run tests with existing recordings (default)
pytest tests/integration/llm/test_streaming.py

# Auto-record missing interactions
LLM_RECORD_MODE=auto pytest tests/integration/llm/test_streaming.py

# Force re-record all interactions
LLM_RECORD_MODE=record pytest tests/integration/llm/test_streaming.py

# Record only Gemini tests
LLM_RECORD_MODE=record GEMINI_API_KEY=xxx pytest tests/integration/llm/ -k gemini
```

### Implementation Details

- **VCR.py (OpenAI)**: YAML cassettes in `tests/cassettes/llm/`
- **DebugConfig (Gemini)**: JSON replays in `tests/cassettes/gemini/`

This provides deterministic testing with full streaming support while maintaining
a single, consistent interface for all providers.
"""

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from family_assistant.llm import LLMInterface, LLMStreamEvent
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.tools.types import ToolDefinition
from tests.factories.messages import (
    create_assistant_message,
    create_system_message,
    create_tool_call,
    create_tool_message,
    create_user_message,
)

from .vcr_helpers import sanitize_response


@pytest_asyncio.fixture
async def llm_client_factory() -> (  # type: ignore[misc]
    AsyncIterator[
        Callable[
            # ast-grep-ignore: no-dict-any - Test infrastructure requires dict config
            [str, str, str | None, dict[str, Any] | None],
            Awaitable[LLMInterface],
        ]
    ]
):
    """Factory fixture for creating LLM clients."""
    # Track created clients so we can close them after tests
    created_clients: list[Any] = []  # Use Any to avoid type issues with close()

    async def _create_client(
        provider: str,
        model: str,
        api_key: str | None = None,
        # ast-grep-ignore: no-dict-any - Test infrastructure requires dict config
        debug_config: dict[str, Any] | None = None,
    ) -> LLMInterface:
        """Create an LLM client for testing."""
        # Use test API key or environment variable
        if api_key is None:
            if provider == "openai":
                api_key = os.getenv("OPENAI_API_KEY", "test-openai-key")
            elif provider == "google":
                api_key = os.getenv("GEMINI_API_KEY", "test-gemini-key")
            elif provider == "anthropic":
                api_key = os.getenv("ANTHROPIC_API_KEY", "test-anthropic-key")
            else:
                api_key = "test-api-key"

        # ast-grep-ignore: no-dict-any - Config dict needs flexible types for factory
        config: dict[str, Any] = {
            "provider": provider,
            "model": model,
            "api_key": api_key,
        }

        # Add provider-specific configuration
        if provider == "google":
            # Use the v1beta endpoint for gemini
            config["api_base"] = "https://generativelanguage.googleapis.com/v1beta"
            # Add debug_config if provided (for record/replay)
            if debug_config:
                config["debug_config"] = debug_config

        client = LLMClientFactory.create_client(config)
        created_clients.append(client)
        return client

    yield _create_client

    # Clean up all created clients
    for client in created_clients:
        if hasattr(client, "close"):
            await client.close()


@pytest_asyncio.fixture
async def sample_tools() -> list[ToolDefinition]:
    """Sample tools for testing tool calling functionality."""
    return [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get current weather for a location",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "The city and country",
                        },
                        "unit": {
                            "type": "string",
                            "enum": ["celsius", "fahrenheit"],
                            "description": "Temperature unit",
                        },
                    },
                    "required": ["location"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "calculate",
                "description": "Perform mathematical calculations",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "expression": {
                            "type": "string",
                            "description": "Mathematical expression to evaluate",
                        }
                    },
                    "required": ["expression"],
                },
            },
        },
    ]


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.parametrize(
    "provider,model",
    [
        ("openai", "gpt-4.1-nano"),
        ("anthropic", "claude-haiku-4-5-20251001"),
    ],
)
async def test_basic_streaming(
    provider: str,
    model: str,
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
    llm_record_mode: str,
) -> None:
    """Test basic streaming functionality for each provider."""
    if llm_record_mode != "replay" and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Recording this test requires {provider.upper()}_API_KEY")

    client = await llm_client_factory(provider, model, None)

    # Simple streaming request
    messages = [
        create_user_message("Count from 1 to 5, with each number on a new line.")
    ]

    # Collect all stream events
    events = []
    accumulated_content = ""
    async for event in client.generate_response_stream(messages):
        assert isinstance(event, LLMStreamEvent)
        events.append(event)
        if event.type == "content" and event.content:
            accumulated_content += event.content

    # Verify we got multiple events
    assert len(events) > 1

    # Verify event types
    content_events = [e for e in events if e.type == "content"]
    assert len(content_events) > 0

    # Should have at least one done event
    done_events = [e for e in events if e.type == "done"]
    assert len(done_events) == 1

    # Check that numbers 1-5 appear in the accumulated content
    full_content = accumulated_content
    for num in ["1", "2", "3", "4", "5"]:
        assert num in full_content


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.parametrize(
    "provider,model",
    [
        ("openai", "gpt-4.1-nano"),
        ("anthropic", "claude-haiku-4-5-20251001"),
    ],
)
async def test_streaming_with_system_message(
    provider: str,
    model: str,
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
    llm_record_mode: str,
) -> None:
    """Test streaming with system messages."""
    if llm_record_mode != "replay" and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Recording this test requires {provider.upper()}_API_KEY")

    client = await llm_client_factory(provider, model, None)

    messages = [
        create_system_message(
            "You are a helpful assistant that responds in a very concise manner."
        ),
        create_user_message(
            "What is the capital of France? Reply in exactly one word."
        ),
    ]

    # Collect all content from stream
    content_parts = []
    accumulated_content = ""
    done_event_received = False

    async for event in client.generate_response_stream(messages):
        assert isinstance(event, LLMStreamEvent)

        if event.type == "content" and event.content:
            content_parts.append(event.content)
            accumulated_content += event.content
        elif event.type == "done":
            done_event_received = True

    # Verify we got content chunks
    assert len(content_parts) > 0

    # Verify done event was received
    assert done_event_received

    # The response should contain "Paris"
    assert "paris" in accumulated_content.lower()


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.parametrize(
    "provider,model",
    [
        ("openai", "gpt-4.1-nano"),
        ("anthropic", "claude-haiku-4-5-20251001"),
    ],
)
async def test_streaming_with_tool_calls(
    provider: str,
    model: str,
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
    sample_tools: list[ToolDefinition],
    llm_record_mode: str,
) -> None:
    """Test streaming with tool calls."""
    if llm_record_mode != "replay" and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Recording this test requires {provider.upper()}_API_KEY")

    client = await llm_client_factory(provider, model, None)

    messages = [
        create_user_message(
            "What's the weather in Paris, France? Also calculate 42 * 17."
        )
    ]

    # Track events
    content_chunks = []
    accumulated_content = ""
    tool_calls = []
    done_event_received = False

    async for event in client.generate_response_stream(
        messages, tools=sample_tools, tool_choice="auto"
    ):
        assert isinstance(event, LLMStreamEvent)

        if event.type == "content" and event.content:
            content_chunks.append(event.content)
            accumulated_content += event.content
        elif event.type == "tool_call" and event.tool_call:
            tool_calls.append(event.tool_call)
        elif event.type == "done":
            done_event_received = True

    # Verify done event was received
    assert done_event_received

    # Should have tool calls
    assert len(tool_calls) > 0

    # Verify tool calls contain expected functions
    tool_names = [tc.function.name for tc in tool_calls]
    assert "get_weather" in tool_names or "calculate" in tool_names


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_gpt_5_6_sol_streaming_with_reasoning_and_tools(
    sample_tools: list[ToolDefinition],
    llm_record_mode: str,
) -> None:
    """Replay the Responses API flow required by GPT-5.6-sol tool calls."""
    if llm_record_mode != "replay" and not os.getenv("OPENAI_API_KEY"):
        pytest.skip("Recording this test requires OPENAI_API_KEY")

    client = LLMClientFactory.create_client({
        "provider": "openai",
        "model": "gpt-5.6-sol",
        "api_key": os.getenv("OPENAI_API_KEY", "test-openai-key"),
        "model_parameters": {
            "gpt-5.6-sol": {
                "reasoning_effort": "low",
                "use_responses_api": True,
            }
        },
    })
    messages = [create_user_message("What is 42 times 17? Use the calculate tool.")]

    tool_calls = []
    done_event = None
    async for event in client.generate_response_stream(
        messages, tools=sample_tools, tool_choice="auto"
    ):
        if event.type == "tool_call" and event.tool_call:
            tool_calls.append(event.tool_call)
        elif event.type == "done":
            done_event = event

    assert [tool_call.function.name for tool_call in tool_calls] == ["calculate"]
    assert done_event is not None
    assert done_event.metadata is not None
    assert "reasoning_info" in done_event.metadata
    provider_metadata = done_event.metadata.get("provider_metadata")
    assert isinstance(provider_metadata, dict)
    assert "openai_response_output" in provider_metadata
    assert provider_metadata["openai_response_stored"] is False

    continuation_messages = [
        *messages,
        create_assistant_message(
            content=None,
            tool_calls=tool_calls,
            provider_metadata=provider_metadata,
        ),
        create_tool_message(
            tool_call_id=tool_calls[0].id,
            content="714",
            name=tool_calls[0].function.name,
        ),
    ]
    continuation_done_event = None
    async for event in client.generate_response_stream(
        continuation_messages, tools=sample_tools, tool_choice="auto"
    ):
        if event.type == "done":
            continuation_done_event = event

    assert continuation_done_event is not None


@pytest.fixture
def require_thinking_cassette(llm_record_mode: str) -> None:
    """Name the missing recording instead of failing on a downstream assertion.

    Without this, replay in a network-less CI fails on `assert tool_calls`: the
    connection error is swallowed and the stream yields nothing, so the failure
    reads as a model that declined to call a tool rather than as a cassette that
    needs recording. Synchronous on purpose -- the filesystem check must not run
    on the event loop.
    """
    cassette = Path(
        "tests/cassettes/llm/test_anthropic_streaming_thinking_round_trip.yaml"
    )
    if llm_record_mode == "replay" and not cassette.exists():
        pytest.fail(
            f"Cassette missing at {cassette}. Record with LLM_RECORD_MODE=record."
        )


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.usefixtures("require_thinking_cassette")
async def test_anthropic_streaming_thinking_round_trip(
    sample_tools: list[ToolDefinition],
    llm_record_mode: str,
) -> None:
    """Thinking blocks survive a tool-use continuation and are replayed verbatim.

    The signature on a thinking block is verified by the API, so a continuation
    that replays a mangled block is rejected. Getting a 200 back on the second
    call is what proves the round trip preserved them byte-for-byte.
    """
    # Gated on the record mode rather than on CI, because the committed cassette
    # replays without a credential. Skipping whenever CI lacks an Anthropic key
    # would mean this assertion -- the whole reason the cassette exists -- never
    # actually runs in CI.
    if llm_record_mode != "replay" and not os.getenv("ANTHROPIC_API_KEY"):
        pytest.skip("Recording this test requires ANTHROPIC_API_KEY")

    # Pinned to the shipped thinking shape, which is what this test exists to
    # exercise: `adaptive` is what the engineer profile sends, and the
    # `enabled` + `budget_tokens` form this test used to pass with a 400 on that
    # generation. The cassette is recorded against `claude-sonnet-5` rather than
    # the `claude-opus-5` the profile now runs -- the two take the identical
    # request shape and return identically structured signed thinking blocks, so
    # the capture-and-replay mechanism under test is the same one either way,
    # and re-recording needs a live Anthropic credential. Re-record against
    # `claude-opus-5` the next time this cassette is regenerated.
    #
    # `display` is the one field set here that the profiles do not set. This
    # generation defaults it to `omitted`, which still returns thinking blocks
    # carrying the signature -- so capture and replay, the mechanism under test,
    # behave identically either way -- but their text is empty, and a turn with
    # no thinking text cannot show that reasoning stays out of the reply. The
    # profiles leave the default because nothing renders reasoning text: the
    # `thinking` stream events are ignored by the processing loop and by the web
    # and iOS transports, so paying for summaries would buy nothing.
    client = LLMClientFactory.create_client({
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "api_key": os.getenv("ANTHROPIC_API_KEY", "test-anthropic-key"),
        "model_parameters": {
            "claude-sonnet-5": {
                "thinking": {"type": "adaptive", "display": "summarized"},
                "output_config": {"effort": "high"},
                "max_tokens": 16000,
            }
        },
    })
    # Adaptive thinking decides per turn whether to think at all, and skips it on
    # a single multiplication -- which recorded a turn with no thinking block to
    # replay. Multi-step arithmetic earns the thinking this test needs on the
    # large majority of runs, but the decision is still the model's: a re-record
    # that lands on a no-thinking turn fails here rather than committing a
    # cassette with nothing to replay, and should just be run again.
    messages = [
        create_user_message(
            "A tank holds 4200 litres. It drains at 17 litres per minute for 42 "
            "minutes, then is refilled at 23 litres per minute for 31 minutes. "
            "Work out the final volume, using the calculate tool for the arithmetic."
        )
    ]

    tool_calls = []
    thinking_chunks = []
    content_chunks = []
    done_event = None
    async for event in client.generate_response_stream(
        messages, tools=sample_tools, tool_choice="auto"
    ):
        if event.type == "tool_call" and event.tool_call:
            tool_calls.append(event.tool_call)
        elif event.type == "thinking" and event.content:
            thinking_chunks.append(event.content)
        elif event.type == "content" and event.content:
            content_chunks.append(event.content)
        elif event.type == "done":
            done_event = event

    assert tool_calls, "expected the model to call a tool"
    assert thinking_chunks, "expected thinking deltas to stream as thinking events"
    # Reasoning must never be folded into the assistant's reply.
    assert not any(chunk in "".join(content_chunks) for chunk in thinking_chunks)

    assert done_event is not None
    assert done_event.metadata is not None
    provider_metadata = done_event.metadata.get("provider_metadata")
    assert isinstance(provider_metadata, dict)
    assert provider_metadata["provider"] == "anthropic"
    thinking_blocks = provider_metadata["thinking_blocks"]
    assert thinking_blocks, "expected thinking blocks captured for replay"
    assert all("signature" in block for block in thinking_blocks)

    continuation_messages = [
        *messages,
        create_assistant_message(
            content=None,
            tool_calls=tool_calls,
            provider_metadata=provider_metadata,
        ),
        create_tool_message(
            tool_call_id=tool_calls[0].id,
            content="714",
            name=tool_calls[0].function.name,
        ),
    ]
    continuation_done_event = None
    async for event in client.generate_response_stream(
        continuation_messages, tools=sample_tools, tool_choice="auto"
    ):
        if event.type == "done":
            continuation_done_event = event

    assert continuation_done_event is not None


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.parametrize(
    "provider,model",
    [
        ("openai", "gpt-4.1-nano"),
        ("anthropic", "claude-haiku-4-5-20251001"),
    ],
)
async def test_streaming_with_multi_turn_conversation(
    provider: str,
    model: str,
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
    llm_record_mode: str,
) -> None:
    """Test streaming with multi-turn conversation."""
    if llm_record_mode != "replay" and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Recording this test requires {provider.upper()}_API_KEY")

    client = await llm_client_factory(provider, model, None)

    messages = [
        create_user_message("My favorite color is blue. Remember this."),
        create_assistant_message("I'll remember that your favorite color is blue."),
        create_user_message("What's my favorite color?"),
    ]

    # Collect complete output
    accumulated_content = ""
    done_event_received = False

    async for event in client.generate_response_stream(messages):
        if event.type == "content" and event.content:
            accumulated_content += event.content
        elif event.type == "done":
            done_event_received = True

    # Verify done event was received
    assert done_event_received

    # Verify response mentions blue
    assert accumulated_content
    assert "blue" in accumulated_content.lower()


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.parametrize(
    "provider,model",
    [
        ("openai", "gpt-4.1-nano"),
        ("anthropic", "claude-haiku-4-5-20251001"),
    ],
)
async def test_streaming_reasoning_info(
    provider: str,
    model: str,
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
    llm_record_mode: str,
) -> None:
    """Test that reasoning info (usage data) is included in streaming responses."""
    if llm_record_mode != "replay" and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Recording this test requires {provider.upper()}_API_KEY")

    client = await llm_client_factory(provider, model, None)

    messages = [create_user_message("Say 'hello world'")]

    accumulated_content = ""
    reasoning_info = None

    async for event in client.generate_response_stream(messages):
        if event.type == "content" and event.content:
            accumulated_content += event.content
        elif (
            event.type == "done"
            and event.metadata
            and "reasoning_info" in event.metadata
        ):
            # Extract reasoning info from metadata if available
            reasoning_info = event.metadata["reasoning_info"]

    # Verify we got content
    assert accumulated_content

    assert reasoning_info is not None
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        token_count = reasoning_info.get(key)
        assert isinstance(token_count, int)
        assert token_count > 0


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.parametrize(
    "provider,model",
    [
        ("openai", "gpt-4.1-nano"),
        ("anthropic", "claude-haiku-4-5-20251001"),
    ],
)
async def test_streaming_content_accumulation(
    provider: str,
    model: str,
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
    llm_record_mode: str,
) -> None:
    """Test that content chunks accumulate correctly to form the complete response."""
    if llm_record_mode != "replay" and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Recording this test requires {provider.upper()}_API_KEY")

    client = await llm_client_factory(provider, model, None)

    messages = [
        create_user_message(
            "Write exactly this text: 'The quick brown fox jumps over the lazy dog.'"
        )
    ]

    # Collect all content chunks
    content_chunks = []
    accumulated_content = ""
    done_event_received = False

    async for event in client.generate_response_stream(messages):
        if event.type == "content" and event.content:
            content_chunks.append(event.content)
            accumulated_content += event.content
        elif event.type == "done":
            done_event_received = True

    # Verify we got content chunks
    assert len(content_chunks) > 0

    # Verify done event was received
    assert done_event_received

    # Check for the expected text
    assert "quick brown fox" in accumulated_content.lower()


# --- Google Gemini Streaming Tests (SDK Record/Replay) ---
# These tests use Google GenAI SDK's built-in DebugConfig for record/replay,
# which natively supports streaming without VCR.py compatibility issues.


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.parametrize(
    "provider,model",
    [
        ("google", "gemini-3.8-flash"),
    ],
)
async def test_basic_streaming_gemini(
    provider: str,
    model: str,
    llm_client_factory: Callable[
        # ast-grep-ignore: no-dict-any - Test infrastructure requires dict config
        [str, str, str | None, dict[str, Any] | None], Awaitable[LLMInterface]
    ],
    # ast-grep-ignore: no-dict-any - Test infrastructure requires dict config
    llm_replay_config: dict[str, Any],
) -> None:
    """Test basic streaming functionality for Google Gemini using SDK record/replay."""
    client = await llm_client_factory(provider, model, None, llm_replay_config)

    # Simple streaming request
    messages = [
        create_user_message("Count from 1 to 5, with each number on a new line.")
    ]

    # Collect all stream events
    events = []
    accumulated_content = ""
    async for event in client.generate_response_stream(messages):
        assert isinstance(event, LLMStreamEvent)
        events.append(event)
        if event.type == "content" and event.content:
            accumulated_content += event.content

    # Verify we got multiple events
    assert len(events) > 1

    # Verify event types
    content_events = [e for e in events if e.type == "content"]
    assert len(content_events) > 0

    # Should have at least one done event
    done_events = [e for e in events if e.type == "done"]
    assert len(done_events) == 1

    # Check that numbers 1-5 appear in the accumulated content
    full_content = accumulated_content
    for num in ["1", "2", "3", "4", "5"]:
        assert num in full_content


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.parametrize(
    "provider,model",
    [
        ("google", "gemini-3.8-flash"),
    ],
)
async def test_streaming_with_system_message_gemini(
    provider: str,
    model: str,
    llm_client_factory: Callable[
        # ast-grep-ignore: no-dict-any - Test infrastructure requires dict config
        [str, str, str | None, dict[str, Any] | None], Awaitable[LLMInterface]
    ],
    # ast-grep-ignore: no-dict-any - Test infrastructure requires dict config
    llm_replay_config: dict[str, Any],
) -> None:
    """Test streaming with system messages for Google Gemini using SDK record/replay."""
    client = await llm_client_factory(provider, model, None, llm_replay_config)

    messages = [
        create_system_message(
            "You are a helpful assistant that responds in a very concise manner."
        ),
        create_user_message(
            "What is the capital of France? Reply in exactly one word."
        ),
    ]

    # Collect all content from stream
    content_parts = []
    accumulated_content = ""
    done_event_received = False

    async for event in client.generate_response_stream(messages):
        assert isinstance(event, LLMStreamEvent)

        if event.type == "content" and event.content:
            content_parts.append(event.content)
            accumulated_content += event.content
        elif event.type == "done":
            done_event_received = True

    # Verify we got content chunks
    assert len(content_parts) > 0

    # Verify done event was received
    assert done_event_received

    # The response should contain "Paris"
    assert "paris" in accumulated_content.lower()


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.parametrize(
    "provider,model",
    [
        ("google", "gemini-3.8-flash"),
    ],
)
async def test_streaming_with_tool_calls_gemini(
    provider: str,
    model: str,
    llm_client_factory: Callable[
        # ast-grep-ignore: no-dict-any - Test infrastructure requires dict config
        [str, str, str | None, dict[str, Any] | None], Awaitable[LLMInterface]
    ],
    sample_tools: list[ToolDefinition],
    # ast-grep-ignore: no-dict-any - Test infrastructure requires dict config
    llm_replay_config: dict[str, Any],
) -> None:
    """Test streaming with tool calls for Google Gemini using SDK record/replay."""
    client = await llm_client_factory(provider, model, None, llm_replay_config)

    messages = [
        create_user_message(
            "What's the weather in Paris, France? Also calculate 42 * 17."
        )
    ]

    # Track events
    content_chunks = []
    accumulated_content = ""
    tool_calls = []
    done_event_received = False

    async for event in client.generate_response_stream(
        messages, tools=sample_tools, tool_choice="auto"
    ):
        assert isinstance(event, LLMStreamEvent)

        if event.type == "content" and event.content:
            content_chunks.append(event.content)
            accumulated_content += event.content
        elif event.type == "tool_call" and event.tool_call:
            tool_calls.append(event.tool_call)
        elif event.type == "done":
            done_event_received = True

    # Verify done event was received
    assert done_event_received

    assert tool_calls
    tool_names = [tc.function.name for tc in tool_calls]
    assert "get_weather" in tool_names or "calculate" in tool_names


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.parametrize(
    "provider,model",
    [
        ("google", "gemini-3.8-flash"),
    ],
)
async def test_streaming_with_multi_turn_conversation_gemini(
    provider: str,
    model: str,
    llm_client_factory: Callable[
        # ast-grep-ignore: no-dict-any - Test infrastructure requires dict config
        [str, str, str | None, dict[str, Any] | None], Awaitable[LLMInterface]
    ],
    # ast-grep-ignore: no-dict-any - Test infrastructure requires dict config
    llm_replay_config: dict[str, Any],
) -> None:
    """Test streaming with multi-turn conversation for Google Gemini using SDK record/replay."""
    client = await llm_client_factory(provider, model, None, llm_replay_config)

    messages = [
        create_user_message("My favorite color is blue. Remember this."),
        create_assistant_message("I'll remember that your favorite color is blue."),
        create_user_message("What's my favorite color?"),
    ]

    # Collect complete output
    accumulated_content = ""
    done_event_received = False

    async for event in client.generate_response_stream(messages):
        if event.type == "content" and event.content:
            accumulated_content += event.content
        elif event.type == "done":
            done_event_received = True

    # Verify done event was received
    assert done_event_received

    # Verify response mentions blue
    assert accumulated_content
    assert "blue" in accumulated_content.lower()


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.parametrize(
    "provider,model",
    [
        ("google", "gemini-3.8-flash"),
    ],
)
async def test_streaming_reasoning_info_gemini(
    provider: str,
    model: str,
    llm_client_factory: Callable[
        # ast-grep-ignore: no-dict-any - Test infrastructure requires dict config
        [str, str, str | None, dict[str, Any] | None], Awaitable[LLMInterface]
    ],
    # ast-grep-ignore: no-dict-any - Test infrastructure requires dict config
    llm_replay_config: dict[str, Any],
) -> None:
    """Test that reasoning info (usage data) is included in streaming responses for Google Gemini using SDK record/replay."""
    client = await llm_client_factory(provider, model, None, llm_replay_config)

    messages = [create_user_message("Say 'hello world'")]

    accumulated_content = ""
    reasoning_info = None

    async for event in client.generate_response_stream(messages):
        if event.type == "content" and event.content:
            accumulated_content += event.content
        elif (
            event.type == "done"
            and event.metadata
            and "reasoning_info" in event.metadata
        ):
            # Extract reasoning info from metadata if available
            reasoning_info = event.metadata["reasoning_info"]

    # Verify we got content
    assert accumulated_content

    assert reasoning_info is not None
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        token_count = reasoning_info.get(key)
        assert isinstance(token_count, int)
        assert token_count > 0


@pytest.mark.no_db
@pytest.mark.llm_integration
async def test_google_streaming_pydantic_validation_reproducer(
    llm_client_factory: Callable[
        # ast-grep-ignore: no-dict-any - Test infrastructure requires dict config
        [str, str, str | None, dict[str, Any] | None], Awaitable[LLMInterface]
    ],
    sample_tools: list[ToolDefinition],
    llm_record_mode: str,
) -> None:
    """Reproducer test for Pydantic validation error with Google GenAI streaming.

    This test is designed to fail with the current bug and pass after the fix.

    Root cause: The google_genai_client._convert_messages_to_genai_format() method
    returns plain dicts with camelCase keys (functionCall, functionResponse) instead
    of the snake_case keys (function_call, function_response) that the Google GenAI
    SDK's Pydantic models expect.

    When streaming with tool calls in conversation history, the SDK's validation
    rejects these malformed dicts with "Extra inputs are not permitted" errors.

    This test will:
    - FAIL initially: Pydantic ValidationError due to camelCase keys in dicts
    - PASS after fix: Snake_case keys allow SDK validation to succeed
    """
    # Create debug config for this specific test (non-parameterized)
    debug_config = {
        "client_mode": llm_record_mode,
        "replay_id": "integration.llm.test_streaming/test_google_streaming_pydantic_validation_reproducer/mldev",
        "replays_directory": "tests/cassettes/gemini",
    }

    client = await llm_client_factory("google", "gemini-3.8-flash", None, debug_config)
    assert isinstance(client, GoogleGenAIClient)

    # Create conversation with tool calls - this triggers the buggy code path
    messages = [
        create_user_message("Calculate 5 + 3"),
        create_assistant_message(
            content=None,
            tool_calls=[
                create_tool_call(
                    call_id="call_test_123",
                    function_name="calculate",
                    arguments='{"expression": "5 + 3"}',
                )
            ],
        ),
        create_tool_message(
            tool_call_id="call_test_123",
            name="calculate",
            content="8",
        ),
    ]

    # Attempt streaming - this should work but will fail with Pydantic validation
    # error if the bug is present
    accumulated_content = ""
    tool_calls = []
    done_received = False

    async def consume_stream() -> None:
        nonlocal accumulated_content
        nonlocal done_received

        async for event in client.generate_response_stream(
            messages, tools=sample_tools, tool_choice="auto"
        ):
            if event.type == "content" and event.content:
                accumulated_content += event.content
            elif event.type == "tool_call" and event.tool_call:
                tool_calls.append(event.tool_call)
            elif event.type == "done":
                done_received = True
            elif event.type == "error":
                pytest.fail(
                    f"Streaming error event received: {event.error}\n"
                    f"This is likely the Pydantic validation error due to camelCase keys"
                )

    try:
        await consume_stream()
    except Exception as e:
        error_msg = str(e)
        # Check if this is the Pydantic validation error we expect
        if "validation" in error_msg.lower() or "Extra inputs" in error_msg:
            pytest.fail(
                f"Pydantic ValidationError caught during streaming:\n{error_msg}\n\n"
                f"This confirms the bug: google_genai_client is passing dicts with "
                f"camelCase keys (functionCall/functionResponse) to the SDK, but the "
                f"SDK expects snake_case keys (function_call/function_response).\n\n"
                f"Fix: Update _convert_messages_to_genai_format() to use snake_case "
                f"or return proper types.Content objects."
            )
        else:
            # Some other unexpected error
            raise

    # Verify we got a proper response
    assert done_received, "Did not receive done event from streaming"
    assert accumulated_content or tool_calls, (
        "No content or tool call received from streaming"
    )
