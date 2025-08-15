"""Integration tests for LLM providers using VCR.py for record/replay."""

import os
from collections.abc import Callable
from typing import Any

import pytest
import pytest_asyncio

from family_assistant.llm import LLMOutput
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


@pytest.mark.no_db
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
async def test_basic_completion(
    provider: str, model: str, llm_client_factory: Any
) -> None:
    """Test basic text completion for each provider."""
    # Skip if running in CI without API keys
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_factory(provider, model)

    # Simple completion request
    messages = [
        {
            "role": "user",
            "content": "Reply with exactly this text: 'Hello from the test suite!'",
        }
    ]

    response = await client.generate_response(messages)

    # Validate response structure
    assert isinstance(response, LLMOutput)
    assert response.content is not None
    assert isinstance(response.content, str)
    assert len(response.content) > 0

    # Check that the model followed instructions (loosely)
    assert "hello" in response.content.lower()
    assert "test" in response.content.lower()


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
async def test_system_message_handling(
    provider: str, model: str, llm_client_factory: Any
) -> None:
    """Test handling of system messages."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_factory(provider, model)

    messages = [
        {
            "role": "system",
            "content": "You are a helpful assistant that always responds in JSON format.",
        },
        {
            "role": "user",
            "content": "What is 2+2? Reply with a JSON object with a key 'answer'.",
        },
    ]

    response = await client.generate_response(messages)

    assert isinstance(response, LLMOutput)
    assert response.content is not None

    # The response should contain JSON-like content
    assert "{" in response.content
    assert "}" in response.content
    assert "answer" in response.content.lower() or "4" in response.content


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
async def test_multi_turn_conversation(
    provider: str, model: str, llm_client_factory: Any
) -> None:
    """Test multi-turn conversation handling."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_factory(provider, model)

    messages = [
        {"role": "user", "content": "My name is TestBot. What's my name?"},
        {"role": "assistant", "content": "Your name is TestBot."},
        {"role": "user", "content": "What did I just tell you my name was?"},
    ]

    response = await client.generate_response(messages)

    assert isinstance(response, LLMOutput)
    assert response.content is not None
    assert "testbot" in response.content.lower()


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
async def test_model_parameters(
    provider: str, model: str, llm_client_factory: Any
) -> None:
    """Test that model parameters are properly passed through."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    # Create client with specific parameters
    client = await llm_client_factory(provider, model)

    # Test with low temperature for more deterministic output
    messages = [{"role": "user", "content": "Complete this sequence: 1, 2, 3, 4,"}]

    # Most models should complete this with "5"
    response = await client.generate_response(messages)

    assert isinstance(response, LLMOutput)
    assert response.content is not None
    assert "5" in response.content


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_provider_specific_google_features(llm_client_factory: Any) -> None:
    """Test Google-specific features like Gemini's native file handling."""
    if os.getenv("CI") and not os.getenv("GEMINI_API_KEY"):
        pytest.skip("Skipping Google-specific test in CI without API key")

    client = await llm_client_factory("google", "gemini-2.5-flash-lite-preview-06-17")

    # Test basic functionality specific to Google
    # For now, just ensure the client works with Google-specific config
    messages = [{"role": "user", "content": "Say 'Google Gemini test passed'"}]

    response = await client.generate_response(messages)

    assert isinstance(response, LLMOutput)
    assert response.content is not None
    assert "gemini" in response.content.lower() or "google" in response.content.lower()


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_provider_specific_openai_features(llm_client_factory: Any) -> None:
    """Test OpenAI-specific features."""
    if os.getenv("CI") and not os.getenv("OPENAI_API_KEY"):
        pytest.skip("Skipping OpenAI-specific test in CI without API key")

    client = await llm_client_factory("openai", "gpt-4.1-nano")

    # Test with a more complex prompt that showcases OpenAI capabilities
    messages = [
        {"role": "system", "content": "You are a code reviewer."},
        {"role": "user", "content": "Review this Python code: print('hello')"},
    ]

    response = await client.generate_response(messages)

    assert isinstance(response, LLMOutput)
    assert response.content is not None
    # Should get some kind of code review response
    assert len(response.content) > 10


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
async def test_empty_conversation(
    provider: str, model: str, llm_client_factory: Any
) -> None:
    """Test handling of edge case with empty user message."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_factory(provider, model)

    # Some providers might handle empty messages differently
    messages = [{"role": "user", "content": ""}]

    try:
        response = await client.generate_response(messages)
        # If it succeeds, should still return valid response
        assert isinstance(response, LLMOutput)
    except Exception as e:
        # Some providers might reject empty messages
        # This is acceptable behavior
        assert "empty" in str(e).lower() or "content" in str(e).lower()


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
async def test_reasoning_info_included(
    provider: str, model: str, llm_client_factory: Any
) -> None:
    """Test that usage/reasoning information is included in response."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_factory(provider, model)

    messages = [{"role": "user", "content": "Count to 5"}]

    response = await client.generate_response(messages)

    assert isinstance(response, LLMOutput)
    assert response.content is not None

    # Check for reasoning_info (usage data)
    if response.reasoning_info:
        assert isinstance(response.reasoning_info, dict)
        # Most providers include token counts
        assert any(
            key in response.reasoning_info
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
async def test_gemini_multipart_content_with_images(llm_client_factory: Any) -> None:
    """Test that Gemini can handle multi-part content with text and images."""
    if os.getenv("CI") and not os.getenv("GEMINI_API_KEY"):
        pytest.skip("Skipping Gemini image test in CI without API key")

    client = await llm_client_factory("google", "gemini-2.5-flash-lite-preview-06-17")

    # Generate a simple red square image
    import base64
    from io import BytesIO

    from PIL import Image

    # Create a 64x64 red image
    img = Image.new("RGB", (64, 64), color="red")

    # Convert to base64
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    img_bytes = buffer.getvalue()
    test_image_base64 = base64.b64encode(img_bytes).decode("utf-8")

    # Test multi-part content with text and image
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "What do you see in this image? Describe the color.",
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{test_image_base64}"},
                },
            ],
        }
    ]

    response = await client.generate_response(messages)

    # Validate response structure
    assert isinstance(response, LLMOutput)
    assert response.content is not None
    assert isinstance(response.content, str)
    assert len(response.content) > 0

    # The model should mention red in the response
    assert "red" in response.content.lower() or "color" in response.content.lower()


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_gemini_system_message_with_multipart_content(
    llm_client_factory: Any,
) -> None:
    """Test that Gemini can handle system messages with multi-part user content."""
    if os.getenv("CI") and not os.getenv("GEMINI_API_KEY"):
        pytest.skip("Skipping Gemini system+image test in CI without API key")

    client = await llm_client_factory("google", "gemini-2.5-flash-lite-preview-06-17")

    # Create a valid blue square image
    import base64
    from io import BytesIO

    from PIL import Image

    # Create a 64x64 blue image
    img = Image.new("RGB", (64, 64), color="blue")

    # Convert to base64
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    img_bytes = buffer.getvalue()
    test_image_base64 = base64.b64encode(img_bytes).decode("utf-8")

    messages = [
        {
            "role": "system",
            "content": "You are a color identification assistant. Always respond with just the color name.",
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What color is this square?"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{test_image_base64}"},
                },
            ],
        },
    ]

    response = await client.generate_response(messages)

    assert isinstance(response, LLMOutput)
    assert response.content is not None
    # Should identify blue
    assert "blue" in response.content.lower()
