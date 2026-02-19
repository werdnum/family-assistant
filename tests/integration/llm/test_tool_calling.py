"""Integration tests for LLM tool calling functionality."""

import json
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from family_assistant.llm import (
    LLMInterface,
    LLMOutput,
    ToolCallFunction,
    ToolCallItem,
)
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.tools.types import ToolDefinition
from tests.factories.messages import (
    create_assistant_message,
    create_tool_message,
    create_user_message,
)

from .vcr_helpers import sanitize_response

if TYPE_CHECKING:
    from family_assistant.llm.messages import LLMMessage


@pytest_asyncio.fixture
async def llm_client_with_tools() -> Callable[[str, str], Awaitable[LLMInterface]]:
    """Factory fixture for creating LLM clients configured for tool calling."""

    async def _create_client(provider: str, model: str) -> LLMInterface:
        """Create an LLM client for tool calling tests."""
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

        # Add provider-specific configuration
        if provider == "google":
            config["api_base"] = "https://generativelanguage.googleapis.com/v1beta"

        return LLMClientFactory.create_client(config)

    return _create_client


def get_weather_tool() -> ToolDefinition:
    """Get a sample weather tool definition."""
    return {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather in a given location",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "The city and state, e.g. San Francisco, CA",
                    },
                    "unit": {
                        "type": "string",
                        "enum": ["celsius", "fahrenheit"],
                        "description": "The temperature unit",
                    },
                },
                "required": ["location"],
            },
        },
    }


def calculate_tool() -> ToolDefinition:
    """Get a sample calculation tool definition."""
    return {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Perform basic mathematical calculations",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "The mathematical expression to evaluate",
                    }
                },
                "required": ["expression"],
            },
        },
    }


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
async def test_single_tool_call(
    provider: str,
    model: str,
    llm_client_with_tools: Callable[[str, str], Awaitable[LLMInterface]],
) -> None:
    """Test calling a single tool."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_with_tools(provider, model)

    tools = [get_weather_tool()]
    messages = [create_user_message("What's the weather like in San Francisco?")]

    response = await client.generate_response(
        messages=messages, tools=tools, tool_choice="auto"
    )

    assert isinstance(response, LLMOutput)
    assert response.tool_calls is not None
    assert len(response.tool_calls) == 1

    tool_call = response.tool_calls[0]
    assert isinstance(tool_call, ToolCallItem)
    assert tool_call.type == "function"
    assert isinstance(tool_call.function, ToolCallFunction)
    assert tool_call.function.name == "get_weather"

    # Parse and validate arguments
    raw_args = tool_call.function.arguments
    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
    assert isinstance(args, dict)
    location = args.get("location")
    assert isinstance(location, str)
    assert "francisco" in location.lower()


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
async def test_multiple_tool_options(
    provider: str,
    model: str,
    llm_client_with_tools: Callable[[str, str], Awaitable[LLMInterface]],
) -> None:
    """Test choosing between multiple available tools."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_with_tools(provider, model)

    tools = [get_weather_tool(), calculate_tool()]
    messages = [create_user_message("What is 25 times 4?")]

    response = await client.generate_response(
        messages=messages, tools=tools, tool_choice="auto"
    )

    assert isinstance(response, LLMOutput)
    assert response.tool_calls is not None
    assert len(response.tool_calls) >= 1

    # Should choose the calculate tool
    tool_call = response.tool_calls[0]
    assert tool_call.function.name == "calculate"

    raw_args = tool_call.function.arguments
    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
    assert isinstance(args, dict)
    expression = args.get("expression")
    assert isinstance(expression, str)
    assert "25" in expression and "4" in expression


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
async def test_no_tool_needed(
    provider: str,
    model: str,
    llm_client_with_tools: Callable[[str, str], Awaitable[LLMInterface]],
) -> None:
    """Test that the model doesn't call tools when not needed."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_with_tools(provider, model)

    tools = [get_weather_tool(), calculate_tool()]
    messages = [create_user_message("Tell me a joke about programming")]

    response = await client.generate_response(
        messages=messages, tools=tools, tool_choice="auto"
    )

    assert isinstance(response, LLMOutput)
    # Should respond with content, not tool calls
    assert response.content is not None
    assert len(response.content) > 0

    # Tool calls should be None or empty
    assert response.tool_calls is None or len(response.tool_calls) == 0


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.parametrize(
    "provider,model",
    [
        ("openai", "gpt-4.1-nano"),
        ("google", "gemini-3-flash-preview"),
    ],
)
async def test_parallel_tool_calls(
    provider: str,
    model: str,
    llm_client_with_tools: Callable[[str, str], Awaitable[LLMInterface]],
) -> None:
    """Test calling multiple tools in parallel (if supported by provider)."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_with_tools(provider, model)

    tools = [get_weather_tool(), calculate_tool()]
    messages = [
        create_user_message("What's the weather in New York and what is 15 plus 27?")
    ]

    response = await client.generate_response(
        messages=messages, tools=tools, tool_choice="auto"
    )

    assert isinstance(response, LLMOutput)
    assert response.tool_calls is not None

    tool_names = [tc.function.name for tc in response.tool_calls]

    # Both tools should be called in a single response (parallel tool calling)
    assert len(tool_names) >= 2, (
        f"Expected at least 2 tool calls in one response for parallel execution, "
        f"got {len(tool_names)}: {tool_names}"
    )
    assert "get_weather" in tool_names
    assert "calculate" in tool_names


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
async def test_tool_call_with_conversation_history(
    provider: str,
    model: str,
    llm_client_with_tools: Callable[[str, str], Awaitable[LLMInterface]],
) -> None:
    """Test tool calling with conversation history."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_with_tools(provider, model)

    tools = [get_weather_tool()]

    # Simulate a conversation with tool usage
    messages = [
        create_user_message("I'm planning a trip to Paris, France"),
        create_assistant_message(
            "Paris is a wonderful destination! Would you like to know about the weather there?"
        ),
        create_user_message("Yes, what's the weather like in Paris, France?"),
    ]

    response = await client.generate_response(
        messages=messages, tools=tools, tool_choice="auto"
    )

    assert isinstance(response, LLMOutput)
    assert response.tool_calls is not None
    assert len(response.tool_calls) >= 1  # May get multiple calls

    # Check that at least one tool call is for get_weather with Paris
    found_paris_weather = False
    for tool_call in response.tool_calls:
        if tool_call.function.name == "get_weather":
            raw_args = tool_call.function.arguments
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            location = args.get("location") if isinstance(args, dict) else None
            if isinstance(location, str) and "paris" in location.lower():
                found_paris_weather = True
                break

    assert found_paris_weather, "Expected at least one get_weather call for Paris"


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
async def test_tool_response_handling(
    provider: str,
    model: str,
    llm_client_with_tools: Callable[[str, str], Awaitable[LLMInterface]],
) -> None:
    """Test handling of tool responses in conversation."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_with_tools(provider, model)

    tools = [get_weather_tool()]

    # First, get a tool call
    messages: list[LLMMessage] = [
        create_user_message("What's the weather in London, UK?")
    ]

    response1 = await client.generate_response(
        messages=messages, tools=tools, tool_choice="auto"
    )

    assert response1.tool_calls is not None
    # Simulate tool execution for every requested tool call. Some providers
    # (notably OpenAI) may emit multiple identical function calls; each one
    # must be answered to satisfy the protocol.
    tool_responses = [
        create_tool_message(
            tool_call_id=tc.id,
            content=json.dumps({
                "location": "London, UK",
                "temperature": 15,
                "unit": "celsius",
                "condition": "Partly cloudy",
            }),
        )
        for tc in response1.tool_calls
    ]

    # Continue conversation with tool response
    assistant_msg = create_assistant_message(
        content=response1.content,
        tool_calls=response1.tool_calls,
    )
    messages.append(assistant_msg)
    messages.extend(tool_responses)

    # Get final response
    response2 = await client.generate_response(messages=messages, tools=tools)

    assert isinstance(response2, LLMOutput)
    assert response2.content is not None

    # Should describe the weather based on tool response
    content_lower = response2.content.lower()
    assert any(
        word in content_lower
        for word in ["london", "15", "celsius", "cloudy", "weather"]
    )


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
async def test_tool_call_id_format(
    provider: str,
    model: str,
    llm_client_with_tools: Callable[[str, str], Awaitable[LLMInterface]],
) -> None:
    """Test that tool call IDs are properly formatted."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_with_tools(provider, model)

    tools = [calculate_tool()]
    messages = [create_user_message("Calculate 42 divided by 7")]

    response = await client.generate_response(
        messages=messages, tools=tools, tool_choice="auto"
    )

    assert response.tool_calls is not None
    assert len(response.tool_calls) > 0

    tool_call = response.tool_calls[0]
    # Tool call should have an ID
    assert tool_call.id is not None
    assert isinstance(tool_call.id, str)
    assert len(tool_call.id) > 0

    # Different providers use different ID formats
    # OpenAI typically uses "call_" prefix
    # Google uses different format
    # Just verify it's a non-empty string


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.parametrize(
    "provider,model",
    [
        # Use a model that is strict about thought signatures
        ("google", "gemini-3.1-pro-preview"),
    ],
)
async def test_gemini_multiturn_without_thought_signature(
    provider: str,
    model: str,
    llm_client_with_tools: Callable[[str, str], Awaitable[LLMInterface]],
) -> None:
    """Test multi-turn tool calling when assistant message has no thought_signature.

    This tests the workaround for Gemini's thought_signature validation requirement.
    When sending a multi-turn conversation where the assistant previously made a
    tool call, but we don't have the original thought_signature (e.g., tool call
    was created programmatically or from a different provider), we need to use
    a dummy signature to satisfy Gemini's validation.

    See: https://ai.google.dev/gemini-api/docs/thought-signatures (FAQ section)
    The docs specify using "skip_thought_signature_validator" as a workaround.
    """
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_with_tools(provider, model)

    tools = [calculate_tool()]

    # Simulate a multi-turn conversation where:
    # 1. User asks a question
    # 2. Assistant made a tool call (but we don't have thought_signature)
    # 3. Tool returned a result
    # 4. Now we want the assistant to respond
    #
    # This is the exact pattern that triggers the thought_signature validation
    # error if we don't provide the workaround.
    messages: list[LLMMessage] = [
        create_user_message("What is 25 * 4?"),
        create_assistant_message(
            content=None,
            tool_calls=[
                ToolCallItem(
                    id="call_test_123",
                    type="function",
                    function=ToolCallFunction(
                        name="calculate",
                        arguments='{"expression": "25 * 4"}',
                    ),
                    # NOTE: No provider_metadata with thought_signature!
                    # This simulates a tool call created without the original signature.
                    provider_metadata=None,
                )
            ],
        ),
        create_tool_message(
            tool_call_id="call_test_123",
            content="100",
        ),
    ]

    # This should NOT fail with thought_signature validation error
    # The client should use the dummy "skip_thought_signature_validator" workaround
    response = await client.generate_response(messages=messages, tools=tools)

    assert isinstance(response, LLMOutput)
    assert response.content is not None
    # Should mention the result in some form
    assert "100" in response.content or "hundred" in response.content.lower()
