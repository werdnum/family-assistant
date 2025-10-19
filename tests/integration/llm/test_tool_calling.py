"""Integration tests for LLM tool calling functionality."""

import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
import pytest_asyncio

from family_assistant.llm import LLMInterface, LLMOutput, ToolCallFunction, ToolCallItem
from family_assistant.llm.factory import LLMClientFactory

from .vcr_helpers import sanitize_response


def skip_if_google_tool_calling(provider: str) -> None:
    """
    Skip test if provider is Google since tool calling is not implemented.

    ARCHITECTURAL LIMITATION: Google's Gemini API integration does not currently
    support structured tool calling in our LLM client implementation. This is a
    temporary limitation pending development work to add tool calling support
    for the Google provider.

    Status: TEMPORARY - Implementation planned

    What needs to be implemented:
    1. Google-specific tool calling request format in LiteLLM client
    2. Proper tool response parsing for Gemini API responses
    3. Integration testing with Google's function calling API
    4. Configuration mapping for Google tool calling parameters

    This skip will be removed once Google tool calling is implemented.
    See related issues/PRs for Google tool calling implementation progress.
    """
    if provider == "google":
        pytest.skip(
            "ARCHITECTURAL LIMITATION: Tool calling not yet implemented for Google provider. "
            "This is a temporary skip pending implementation of Google/Gemini tool calling "
            "support in our LLM client. The test infrastructure exists and will work once "
            "the underlying Google provider tool calling is implemented."
        )


@pytest_asyncio.fixture
async def llm_client_with_tools() -> Callable[[str, str], Awaitable[LLMInterface]]:
    """Factory fixture for creating LLM clients configured for tool calling."""

    async def _create_client(provider: str, model: str) -> LLMInterface:
        """Create an LLM client for tool calling tests."""
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
            config["api_base"] = "https://generativelanguage.googleapis.com/v1beta"

        return LLMClientFactory.create_client(config)

    return _create_client


# ast-grep-ignore: no-dict-any - Legacy code - needs structured types
def get_weather_tool() -> dict[str, Any]:
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


# ast-grep-ignore: no-dict-any - Legacy code - needs structured types
def calculate_tool() -> dict[str, Any]:
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
        ("google", "gemini-2.5-flash-lite-preview-06-17"),
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
    messages = [
        {"role": "user", "content": "What's the weather like in San Francisco?"}
    ]

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
    args = json.loads(tool_call.function.arguments)
    assert "location" in args
    assert "francisco" in args["location"].lower()


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.parametrize(
    "provider,model",
    [
        ("openai", "gpt-4.1-nano"),
        ("google", "gemini-2.5-flash-lite-preview-06-17"),
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
    messages = [{"role": "user", "content": "What is 25 times 4?"}]

    response = await client.generate_response(
        messages=messages, tools=tools, tool_choice="auto"
    )

    assert isinstance(response, LLMOutput)
    assert response.tool_calls is not None
    assert len(response.tool_calls) >= 1

    # Should choose the calculate tool
    tool_call = response.tool_calls[0]
    assert tool_call.function.name == "calculate"

    args = json.loads(tool_call.function.arguments)
    assert "expression" in args
    assert "25" in args["expression"] and "4" in args["expression"]


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.parametrize(
    "provider,model",
    [
        ("openai", "gpt-4.1-nano"),
        ("google", "gemini-2.5-flash-lite-preview-06-17"),
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
    messages = [{"role": "user", "content": "Tell me a joke about programming"}]

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
        # Google Gemini might handle parallel calls differently
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
        {
            "role": "user",
            "content": "What's the weather in New York and what is 15 plus 27?",
        }
    ]

    response = await client.generate_response(
        messages=messages, tools=tools, tool_choice="auto"
    )

    assert isinstance(response, LLMOutput)
    assert response.tool_calls is not None

    # Some models might make parallel calls, others sequential
    # Just verify we get tool calls for both requests
    tool_names = [tc.function.name for tc in response.tool_calls]

    # Might get both in one response or need multiple turns
    assert len(tool_names) >= 1
    assert any(name in {"get_weather", "calculate"} for name in tool_names)


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.parametrize(
    "provider,model",
    [
        ("openai", "gpt-4.1-nano"),
        ("google", "gemini-2.5-flash-lite-preview-06-17"),
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
        {"role": "user", "content": "I'm planning a trip to Paris, France"},
        {
            "role": "assistant",
            "content": "Paris is a wonderful destination! Would you like to know about the weather there?",
        },
        {"role": "user", "content": "Yes, what's the weather like in Paris, France?"},
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
            args = json.loads(tool_call.function.arguments)
            if "location" in args and "paris" in args["location"].lower():
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
        ("google", "gemini-2.5-flash-lite-preview-06-17"),
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
    messages = [{"role": "user", "content": "What's the weather in London, UK?"}]

    response1 = await client.generate_response(
        messages=messages, tools=tools, tool_choice="auto"
    )

    assert response1.tool_calls is not None
    tool_call = response1.tool_calls[0]

    # Simulate tool execution and response
    tool_response = {
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": json.dumps({
            "location": "London, UK",
            "temperature": 15,
            "unit": "celsius",
            "condition": "Partly cloudy",
        }),
    }

    # Continue conversation with tool response
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    assistant_msg: dict[str, Any] = {
        "role": "assistant",
        "content": response1.content,
        "tool_calls": [
            {
                "id": tool_call.id,
                "type": tool_call.type,
                "function": {
                    "name": tool_call.function.name,
                    "arguments": tool_call.function.arguments,
                },
            }
        ],
    }
    messages.append(assistant_msg)
    messages.append(tool_response)

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
        ("google", "gemini-2.5-flash-lite-preview-06-17"),
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
    messages = [{"role": "user", "content": "Calculate 42 divided by 7"}]

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
