"""Integration tests for LLM streaming functionality using VCR.py for record/replay.

TODO: Google/Gemini streaming tests are currently skipped due to VCR.py incompatibility
with aiohttp streaming responses. The MockStream object created by VCR.py doesn't implement
the `readany()` method required by aiohttp 3.12+. This is a known issue (VCR.py #927).

Potential solutions:
1. Wait for VCR.py to fix the streaming compatibility
2. Use httpx instead of aiohttp for Google GenAI client
3. Implement custom mocking for streaming tests
4. Test streaming without VCR.py (direct API calls)
"""

import os
from collections.abc import Callable
from typing import Any

import pytest
import pytest_asyncio

from family_assistant.llm import LLMStreamEvent
from family_assistant.llm.factory import LLMClientFactory

from .vcr_helpers import sanitize_response


@pytest_asyncio.fixture
async def llm_client_factory() -> Callable[[str, str, str | None], Any]:
    """Factory fixture for creating LLM clients."""

    async def _create_client(
        provider: str, model: str, api_key: str | None = None
    ) -> Any:
        """Create an LLM client for testing."""
        # Use test API key or environment variable
        if api_key is None:
            if provider == "openai":
                api_key = os.getenv("OPENAI_API_KEY", "test-openai-key")
            elif provider == "google":
                api_key = os.getenv("GEMINI_API_KEY", "test-gemini-key")
            else:
                api_key = "test-api-key"

        config = {
            "provider": provider,
            "model": model,
            "api_key": api_key,
        }

        # Add provider-specific configuration
        if provider == "google":
            # Use the v1beta endpoint for gemini
            config["api_base"] = "https://generativelanguage.googleapis.com/v1beta"

        return LLMClientFactory.create_client(config)

    return _create_client


@pytest_asyncio.fixture
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
        pytest.param(
            "google",
            "gemini-2.5-flash-lite-preview-06-17",
            marks=pytest.mark.skip(
                reason="VCR.py MockStream incompatibility with aiohttp streaming. "
                "TODO: Implement workaround for VCR.py issue #927"
            ),
        ),
    ],
)
async def test_basic_streaming(
    provider: str, model: str, llm_client_factory: Any
) -> None:
    """Test basic streaming functionality for each provider."""
    # Skip if running in CI without API keys
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_factory(provider, model)

    # Simple streaming request
    messages = [
        {
            "role": "user",
            "content": "Count from 1 to 5, with each number on a new line.",
        }
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
        pytest.param(
            "google",
            "gemini-2.5-flash-lite-preview-06-17",
            marks=pytest.mark.skip(
                reason="VCR.py MockStream incompatibility with aiohttp streaming. "
                "TODO: Implement workaround for VCR.py issue #927"
            ),
        ),
    ],
)
async def test_streaming_with_system_message(
    provider: str, model: str, llm_client_factory: Any
) -> None:
    """Test streaming with system messages."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_factory(provider, model)

    messages = [
        {
            "role": "system",
            "content": "You are a helpful assistant that responds in a very concise manner.",
        },
        {
            "role": "user",
            "content": "What is the capital of France? Reply in exactly one word.",
        },
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
        pytest.param(
            "google",
            "gemini-2.5-flash-lite-preview-06-17",
            marks=pytest.mark.skip(
                reason="VCR.py MockStream incompatibility with aiohttp streaming. "
                "TODO: Implement workaround for VCR.py issue #927"
            ),
        ),
    ],
)
async def test_streaming_with_tool_calls(
    provider: str,
    model: str,
    llm_client_factory: Any,
    sample_tools: list[dict[str, Any]],
) -> None:
    """Test streaming with tool calls."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_factory(provider, model)

    messages = [
        {
            "role": "user",
            "content": "What's the weather in Paris, France? Also calculate 42 * 17.",
        }
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
        pytest.param(
            "google",
            "gemini-2.5-flash-lite-preview-06-17",
            marks=pytest.mark.skip(
                reason="VCR.py MockStream incompatibility with aiohttp streaming. "
                "TODO: Implement workaround for VCR.py issue #927"
            ),
        ),
    ],
)
async def test_streaming_error_handling(
    provider: str, model: str, llm_client_factory: Any
) -> None:
    """Test error handling during streaming."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_factory(provider, model)

    # Test with invalid message format (missing role)
    messages = [
        {
            "content": "This message is missing the role field",
        }
    ]

    error_event_received = False
    error_message = None

    try:
        async for event in client.generate_response_stream(messages):
            if event.type == "error":
                error_event_received = True
                error_message = event.data
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
        pytest.param(
            "google",
            "gemini-2.5-flash-lite-preview-06-17",
            marks=pytest.mark.skip(
                reason="VCR.py MockStream incompatibility with aiohttp streaming. "
                "TODO: Implement workaround for VCR.py issue #927"
            ),
        ),
    ],
)
async def test_streaming_with_multi_turn_conversation(
    provider: str, model: str, llm_client_factory: Any
) -> None:
    """Test streaming with multi-turn conversation."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_factory(provider, model)

    messages = [
        {"role": "user", "content": "My favorite color is blue. Remember this."},
        {
            "role": "assistant",
            "content": "I'll remember that your favorite color is blue.",
        },
        {"role": "user", "content": "What's my favorite color?"},
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
        pytest.param(
            "google",
            "gemini-2.5-flash-lite-preview-06-17",
            marks=pytest.mark.skip(
                reason="VCR.py MockStream incompatibility with aiohttp streaming. "
                "TODO: Implement workaround for VCR.py issue #927"
            ),
        ),
    ],
)
async def test_streaming_reasoning_info(
    provider: str, model: str, llm_client_factory: Any
) -> None:
    """Test that reasoning info (usage data) is included in streaming responses."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_factory(provider, model)

    messages = [{"role": "user", "content": "Say 'hello world'"}]

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
    provider: str, model: str, llm_client_factory: Any
) -> None:
    """Test that content chunks accumulate correctly to form the complete response."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_factory(provider, model)

    messages = [
        {
            "role": "user",
            "content": "Write exactly this text: 'The quick brown fox jumps over the lazy dog.'",
        }
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
async def test_litellm_streaming_with_various_models(llm_client_factory: Any) -> None:
    """Test LiteLLM streaming with various model configurations."""
    # This test uses a mock provider to avoid API calls
    # We'll test the LiteLLM client directly with streaming support

    # Skip in CI without API keys
    if os.getenv("CI"):
        pytest.skip("Skipping LiteLLM streaming test in CI")

    # Test with a LiteLLM-supported model
    client = await llm_client_factory("litellm", "gpt-4.1-nano")

    messages = [
        {
            "role": "user",
            "content": "Reply with exactly: 'LiteLLM streaming works!'",
        }
    ]

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
