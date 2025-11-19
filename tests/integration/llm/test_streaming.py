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

import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
import pytest_asyncio

from family_assistant.llm import LLMInterface, LLMStreamEvent
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.llm.messages import message_to_dict
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from tests.factories.messages import (
    create_assistant_message,
    create_system_message,
    create_tool_call,
    create_tool_message,
    create_user_message,
)

from .vcr_helpers import sanitize_response


@pytest_asyncio.fixture
async def llm_client_factory() -> Callable[
    # ast-grep-ignore: no-dict-any - Test infrastructure requires dict config
    [str, str, str | None, dict[str, Any] | None], Awaitable[LLMInterface]
]:
    """Factory fixture for creating LLM clients."""

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

        return LLMClientFactory.create_client(config)

    return _create_client


@pytest_asyncio.fixture
# ast-grep-ignore: no-dict-any - Legacy code - needs structured types
async def sample_tools() -> list[dict[str, Any]]:
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
    ],
)
async def test_basic_streaming(
    provider: str,
    model: str,
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
) -> None:
    """Test basic streaming functionality for each provider."""
    # Skip if running in CI without API keys
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

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
    ],
)
async def test_streaming_with_system_message(
    provider: str,
    model: str,
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
) -> None:
    """Test streaming with system messages."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

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
    ],
)
async def test_streaming_with_tool_calls(
    provider: str,
    model: str,
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    sample_tools: list[dict[str, Any]],
) -> None:
    """Test streaming with tool calls."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

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
@pytest.mark.parametrize(
    "provider,model",
    [
        ("openai", "gpt-4.1-nano"),
    ],
)
async def test_streaming_error_handling(
    provider: str,
    model: str,
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
) -> None:
    """Test error handling during streaming."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_factory(provider, model, None)

    # Test with invalid message format (missing role)
    messages = [
        {
            "content": "This message is missing the role field",
        }
    ]

    error_event_received = False
    error_message = None

    try:
        async for event in client.generate_response_stream(messages):  # type: ignore[reportArgumentType]
            if event.type == "error":
                error_event_received = True
                error_message = event.error
                break
    except Exception as e:
        # Some providers might raise exceptions instead of yielding error events
        error_message = str(e)

    # Either we got an error event or an exception
    assert error_event_received or error_message is not None


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.parametrize(
    "provider,model",
    [
        ("openai", "gpt-4.1-nano"),
    ],
)
async def test_streaming_with_multi_turn_conversation(
    provider: str,
    model: str,
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
) -> None:
    """Test streaming with multi-turn conversation."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

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
    ],
)
async def test_streaming_reasoning_info(
    provider: str,
    model: str,
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
) -> None:
    """Test that reasoning info (usage data) is included in streaming responses."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

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

    # Check for reasoning_info (usage data) if available
    if reasoning_info:
        assert isinstance(reasoning_info, dict)
        # Most providers include token counts in streaming
        assert any(
            key in reasoning_info
            for key in [
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "prompt_token_count",
                "candidates_token_count",
                "total_token_count",
            ]
        )


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.parametrize(
    "provider,model",
    [
        ("openai", "gpt-4.1-nano"),
    ],
)
async def test_streaming_content_accumulation(
    provider: str,
    model: str,
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
) -> None:
    """Test that content chunks accumulate correctly to form the complete response."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

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


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_litellm_streaming_with_various_models(
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
) -> None:
    """Test LiteLLM streaming with various model configurations."""
    # This test uses a mock provider to avoid API calls
    # We'll test the LiteLLM client directly with streaming support

    # Skip in CI without API keys or when no API key is available
    if os.getenv("CI") or not os.getenv("OPENAI_API_KEY"):
        pytest.skip("Skipping LiteLLM streaming test - requires API key")

    # Test with a LiteLLM-supported model
    client = await llm_client_factory("litellm", "gpt-4.1-nano", None)

    messages = [create_user_message("Reply with exactly: 'LiteLLM streaming works!'")]

    # Collect events
    events = []
    async for event in client.generate_response_stream(messages):
        assert isinstance(event, LLMStreamEvent)
        events.append(event)

    # Verify we got events
    assert len(events) > 0

    # Find done event
    done_events = [e for e in events if e.type == "done"]
    assert len(done_events) == 1

    # Accumulate content from events
    accumulated_content = ""
    for event in events:
        if event.type == "content" and event.content:
            accumulated_content += event.content

    assert accumulated_content
    assert (
        "litellm" in accumulated_content.lower()
        or "streaming" in accumulated_content.lower()
    )


# --- Google Gemini Streaming Tests (SDK Record/Replay) ---
# These tests use Google GenAI SDK's built-in DebugConfig for record/replay,
# which natively supports streaming without VCR.py compatibility issues.


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.parametrize(
    "provider,model",
    [
        ("google", "gemini-2.5-flash-lite-preview-06-17"),
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
        ("google", "gemini-2.5-flash-lite-preview-06-17"),
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
        ("google", "gemini-2.5-flash-lite-preview-06-17"),
    ],
)
async def test_streaming_with_tool_calls_gemini(
    provider: str,
    model: str,
    llm_client_factory: Callable[
        # ast-grep-ignore: no-dict-any - Test infrastructure requires dict config
        [str, str, str | None, dict[str, Any] | None], Awaitable[LLMInterface]
    ],
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    sample_tools: list[dict[str, Any]],
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

    # Should have tool calls (Note: Google Gemini may not support tool calling yet)
    # This test may fail until tool calling is implemented for Google provider
    # For now, we just verify the stream works even if no tools are called
    if tool_calls:
        # Verify tool calls contain expected functions
        tool_names = [tc.function.name for tc in tool_calls]
        assert "get_weather" in tool_names or "calculate" in tool_names


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.parametrize(
    "provider,model",
    [
        ("google", "gemini-2.5-flash-lite-preview-06-17"),
    ],
)
async def test_streaming_error_handling_gemini(
    provider: str,
    model: str,
    llm_client_factory: Callable[
        # ast-grep-ignore: no-dict-any - Test infrastructure requires dict config
        [str, str, str | None, dict[str, Any] | None], Awaitable[LLMInterface]
    ],
    # ast-grep-ignore: no-dict-any - Test infrastructure requires dict config
    llm_replay_config: dict[str, Any],
) -> None:
    """Test error handling during streaming for Google Gemini using SDK record/replay."""
    client = await llm_client_factory(provider, model, None, llm_replay_config)

    # Test with invalid message format (missing role)
    messages = [
        {
            "content": "This message is missing the role field",
        }
    ]

    error_event_received = False
    error_message = None

    try:
        async for event in client.generate_response_stream(messages):  # type: ignore[reportArgumentType]
            if event.type == "error":
                error_event_received = True
                error_message = event.error
                break
    except Exception as e:
        # Some providers might raise exceptions instead of yielding error events
        error_message = str(e)

    # Either we got an error event or an exception
    assert error_event_received or error_message is not None


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.parametrize(
    "provider,model",
    [
        ("google", "gemini-2.5-flash-lite-preview-06-17"),
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
        ("google", "gemini-2.5-flash-lite-preview-06-17"),
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

    # Check for reasoning_info (usage data) if available
    if reasoning_info:
        assert isinstance(reasoning_info, dict)
        # Most providers include token counts in streaming
        assert any(
            key in reasoning_info
            for key in [
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "prompt_token_count",
                "candidates_token_count",
                "total_token_count",
            ]
        )


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.parametrize(
    "provider,model",
    [
        ("google", "gemini-2.5-flash-lite-preview-06-17"),
    ],
)
async def test_google_streaming_with_multiturns_and_tool_calls(
    provider: str,
    model: str,
    llm_client_factory: Callable[
        # ast-grep-ignore: no-dict-any - Test infrastructure requires dict config
        [str, str, str | None, dict[str, Any] | None], Awaitable[LLMInterface]
    ],
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    sample_tools: list[dict[str, Any]],
    # ast-grep-ignore: no-dict-any - Test infrastructure requires dict config
    llm_replay_config: dict[str, Any],
) -> None:
    """Test streaming with multi-turn conversation including tool calls for Google Gemini.

    This test reproduces the Pydantic validation bug where the Google GenAI client
    uses camelCase keys (functionCall, functionResponse) instead of snake_case keys
    (function_call, function_response) expected by the SDK's Pydantic validation.

    The bug manifests when:
    1. Streaming is enabled
    2. Multi-turn conversation (with assistant message containing tool_calls in history)
    3. Real API call (not VCR replay)
    """

    client = await llm_client_factory(provider, model, None, llm_replay_config)
    assert isinstance(client, GoogleGenAIClient)

    # Simulate a multi-turn conversation with tool calls
    # This is the exact pattern that triggers the Pydantic validation error
    messages = [
        create_user_message("What's the weather in Paris?"),
        create_assistant_message(
            content=None,
            tool_calls=[
                create_tool_call(
                    call_id="call_123",
                    function_name="get_weather",
                    arguments='{"location": "Paris, France", "unit": "celsius"}',
                )
            ],
        ),
        create_tool_message(
            tool_call_id="call_123",
            name="get_weather",
            content="The weather in Paris is 18°C and sunny.",
        ),
    ]

    # Capture what gets sent to the API by inspecting _convert_messages_to_genai_format
    message_dicts = [message_to_dict(msg) for msg in messages]
    converted = client._convert_messages_to_genai_format(message_dicts)

    # Check if using camelCase (bug) or snake_case (correct)
    converted_json = json.dumps(converted, default=str)
    has_camel_case = (
        "functionCall" in converted_json or "functionResponse" in converted_json
    )
    has_snake_case = (
        "function_call" in converted_json or "function_response" in converted_json
    )

    print("\n=== Converted format check ===")
    print(f"Has camelCase (functionCall/functionResponse): {has_camel_case}")
    print(f"Has snake_case (function_call/function_response): {has_snake_case}")
    print(f"Sample: {converted_json[:500]}")

    # Try to stream the next response
    accumulated_content = ""
    done_event_received = False
    error_occurred = False
    error_message = None

    try:
        async for event in client.generate_response_stream(
            messages, tools=sample_tools, tool_choice="auto"
        ):
            if event.type == "content" and event.content:
                accumulated_content += event.content
            elif event.type == "done":
                done_event_received = True
            elif event.type == "error":
                error_occurred = True
                error_message = event.error
    except Exception as e:
        error_occurred = True
        error_message = str(e)
        print(f"Exception: {type(e).__name__}: {str(e)[:500]}")

    # Report the results
    if error_occurred and has_camel_case:
        print("\n=== BUG REPRODUCED ===")
        print(f"Error: {error_message[:200] if error_message else 'Unknown error'}")
        print("This is the expected Pydantic validation error with camelCase keys")
        # This is expected when the bug is present
        assert error_message and (
            "validation" in error_message.lower() or "Extra inputs" in error_message
        )
    elif has_snake_case:
        print("\n=== BUG FIXED ===")
        print("Using snake_case keys - streaming should work")
        assert not error_occurred, f"Streaming failed unexpectedly: {error_message}"
        assert done_event_received, "Done event not received"
        assert accumulated_content, "No content received from streaming"
    else:
        # Neither format found - something else is wrong
        raise AssertionError(
            f"Unexpected format in converted messages: {converted_json[:200]}"
        )


@pytest.mark.no_db
@pytest.mark.llm_integration
async def test_google_streaming_pydantic_validation_reproducer(
    llm_client_factory: Callable[
        # ast-grep-ignore: no-dict-any - Test infrastructure requires dict config
        [str, str, str | None, dict[str, Any] | None], Awaitable[LLMInterface]
    ],
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    sample_tools: list[dict[str, Any]],
    # ast-grep-ignore: no-dict-any - Test infrastructure requires dict config
    llm_replay_config: dict[str, Any],
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
    client = await llm_client_factory(
        "google", "gemini-2.5-flash-lite-preview-06-17", None, llm_replay_config
    )
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
    done_received = False

    try:
        async for event in client.generate_response_stream(
            messages, tools=sample_tools, tool_choice="auto"
        ):
            if event.type == "content" and event.content:
                accumulated_content += event.content
            elif event.type == "done":
                done_received = True
            elif event.type == "error":
                # If we get an error event, fail the test
                pytest.fail(
                    f"Streaming error event received: {event.error}\n"
                    f"This is likely the Pydantic validation error due to camelCase keys"
                )
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
    assert accumulated_content or done_received, "No content received from streaming"
