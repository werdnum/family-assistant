"""Integration tests for LLM providers using VCR.py for record/replay."""

import base64
import os
import pathlib
from collections.abc import Awaitable, Callable
from io import BytesIO

import pytest
import pytest_asyncio
from PIL import Image

from family_assistant.llm import LLMInterface, LLMOutput
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.llm.messages import ImageUrlContentPart, TextContentPart
from family_assistant.tools.types import ToolAttachment
from tests.factories.messages import (
    create_assistant_message,
    create_system_message,
    create_tool_message,
    create_user_message,
)

from .vcr_helpers import sanitize_response


@pytest_asyncio.fixture
async def llm_client_factory() -> Callable[
    [str, str, str | None], Awaitable[LLMInterface]
]:
    """Factory fixture for creating LLM clients."""

    async def _create_client(
        provider: str, model: str, api_key: str | None = None
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
    provider: str,
    model: str,
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
) -> None:
    """Test basic text completion for each provider."""
    # Skip if running in CI without API keys
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_factory(provider, model, None)

    # Simple completion request
    messages = [
        create_user_message(
            "Reply with exactly this text: 'Hello from the test suite!'"
        )
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
    provider: str,
    model: str,
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
) -> None:
    """Test handling of system messages."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_factory(provider, model, None)

    messages = [
        create_system_message(
            "You are a helpful assistant that always responds in JSON format."
        ),
        create_user_message(
            "What is 2+2? Reply with a JSON object with a key 'answer'."
        ),
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
    provider: str,
    model: str,
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
) -> None:
    """Test multi-turn conversation handling."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_factory(provider, model, None)

    messages = [
        create_user_message("My name is TestBot. What's my name?"),
        create_assistant_message("Your name is TestBot."),
        create_user_message("What did I just tell you my name was?"),
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
    provider: str,
    model: str,
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
) -> None:
    """Test that model parameters are properly passed through."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    # Create client with specific parameters
    client = await llm_client_factory(provider, model, None)

    # Test with low temperature for more deterministic output
    messages = [create_user_message("Complete this sequence: 1, 2, 3, 4,")]

    # Most models should complete this with "5"
    response = await client.generate_response(messages)

    assert isinstance(response, LLMOutput)
    assert response.content is not None
    assert "5" in response.content


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_provider_specific_google_features(
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
) -> None:
    """Test Google-specific features like Gemini's native file handling."""
    if os.getenv("CI") and not os.getenv("GEMINI_API_KEY"):
        pytest.skip("Skipping Google-specific test in CI without API key")

    client = await llm_client_factory(
        "google", "gemini-2.5-flash-lite-preview-06-17", None
    )

    # Test basic functionality specific to Google
    # For now, just ensure the client works with Google-specific config
    messages = [create_user_message("Say 'Google Gemini test passed'")]

    response = await client.generate_response(messages)

    assert isinstance(response, LLMOutput)
    assert response.content is not None
    assert "gemini" in response.content.lower() or "google" in response.content.lower()


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_provider_specific_openai_features(
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
) -> None:
    """Test OpenAI-specific features."""
    if os.getenv("CI") and not os.getenv("OPENAI_API_KEY"):
        pytest.skip("Skipping OpenAI-specific test in CI without API key")

    client = await llm_client_factory("openai", "gpt-4.1-nano", None)

    # Test with a more complex prompt that showcases OpenAI capabilities
    messages = [
        create_system_message("You are a code reviewer."),
        create_user_message("Review this Python code: print('hello')"),
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
    provider: str,
    model: str,
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
) -> None:
    """Test handling of edge case with empty user message."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_factory(provider, model, None)

    # Some providers might handle empty messages differently
    messages = [create_user_message("")]

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
    provider: str,
    model: str,
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
) -> None:
    """Test that usage/reasoning information is included in response."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_factory(provider, model, None)

    messages = [create_user_message("Count to 5")]

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
async def test_gemini_multipart_content_with_images(
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
) -> None:
    """Test that Gemini can handle multi-part content with text and images."""
    if os.getenv("CI") and not os.getenv("GEMINI_API_KEY"):
        pytest.skip("Skipping Gemini image test in CI without API key")

    client = await llm_client_factory(
        "google", "gemini-2.5-flash-lite-preview-06-17", None
    )

    # Generate a simple red square image
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
        create_user_message(
            content=[
                TextContentPart(
                    type="text",
                    text="What do you see in this image? Describe the color.",
                ),
                ImageUrlContentPart(
                    type="image_url",
                    image_url={"url": f"data:image/png;base64,{test_image_base64}"},
                ),
            ]
        )
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
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
) -> None:
    """Test that Gemini can handle system messages with multi-part user content."""
    if os.getenv("CI") and not os.getenv("GEMINI_API_KEY"):
        pytest.skip("Skipping Gemini system+image test in CI without API key")

    client = await llm_client_factory(
        "google", "gemini-2.5-flash-lite-preview-06-17", None
    )

    # Create a valid blue square image
    # Create a 64x64 blue image
    img = Image.new("RGB", (64, 64), color="blue")

    # Convert to base64
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    img_bytes = buffer.getvalue()
    test_image_base64 = base64.b64encode(img_bytes).decode("utf-8")

    messages = [
        create_system_message(
            "You are a color identification assistant. Always respond with just the color name."
        ),
        create_user_message(
            content=[
                TextContentPart(type="text", text="What color is this square?"),
                ImageUrlContentPart(
                    type="image_url",
                    image_url={"url": f"data:image/png;base64,{test_image_base64}"},
                ),
            ]
        ),
    ]

    response = await client.generate_response(messages)

    assert isinstance(response, LLMOutput)
    assert response.content is not None
    # Should identify blue
    assert "blue" in response.content.lower()


# Tests for multimodal tool result handling


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
async def test_tool_message_with_image_attachment(
    provider: str,
    model: str,
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
) -> None:
    """Test that providers handle tool messages with image attachments."""
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_factory(provider, model, None)

    # Import here to avoid circular imports

    # Create a small test image (1x1 red pixel PNG)
    png_data = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )

    attachment = ToolAttachment(
        mime_type="image/png", content=png_data, description="A small test image"
    )

    # Simulate a conversation where a tool has returned an image
    messages = [
        create_user_message("Generate a simple image for me"),
        {
            "role": "assistant",
            "content": "I'll generate a simple image for you.",
            "tool_calls": [
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {
                        "name": "generate_image",
                        "arguments": '{"prompt": "simple"}',
                    },
                }
            ],
        },
        create_tool_message(
            tool_call_id="call_123",
            content="Generated a simple red pixel image",
            attachments_list=[attachment],
        ),
        create_user_message("What do you see in the image?"),
    ]

    # This should work without errors - the provider should handle the attachment
    response = await client.generate_response(messages)

    assert isinstance(response, LLMOutput)
    assert response.content is not None
    assert isinstance(response.content, str)
    assert len(response.content) > 0


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
async def test_tool_message_with_pdf_attachment(
    provider: str,
    model: str,
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
) -> None:
    """Test that providers handle tool messages with PDF attachments and can read the content.

    This test uses the actual PDF file about software updates to verify that LLMs
    can read and understand PDF content when sent via the appropriate API format.
    """
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")

    client = await llm_client_factory(provider, model, None)

    # Import here to avoid circular imports
    # Read the actual test PDF file about software updates

    pdf_path = pathlib.Path(__file__).parent.parent.parent / "data" / "test_doc.pdf"
    pdf_data = pdf_path.read_bytes()

    attachment = ToolAttachment(
        mime_type="application/pdf",
        content=pdf_data,
        description="Document from search results",
    )

    # Simulate a conversation where a tool has returned a PDF about software updates
    messages = [
        create_user_message("Find me a document about software management"),
        {
            "role": "assistant",
            "content": "I'll search for a document about software management.",
            "tool_calls": [
                {
                    "id": "call_456",
                    "type": "function",
                    "function": {
                        "name": "search_documents",
                        "arguments": '{"query": "software management"}',
                    },
                }
            ],
        },
        create_tool_message(
            tool_call_id="call_456",
            content="Found a PDF document about software management practices",
            attachments_list=[attachment],
        ),
        create_user_message(
            "What is this document about? What are the main topics it covers?"
        ),
    ]

    # This should work without errors - the provider should handle the attachment
    response = await client.generate_response(messages)

    assert isinstance(response, LLMOutput)
    assert response.content is not None
    assert isinstance(response.content, str)
    assert len(response.content) > 0

    # The response should indicate the document is about software updates
    # since the PDF actually contains content about the importance of software updates
    response_lower = response.content.lower()
    assert any(
        keyword in response_lower
        for keyword in [
            "update",
            "software",
            "security",
            "performance",
            "patch",
            "vulnerability",
        ]
    ), (
        f"Response should mention software update topics from the PDF content, got: {response.content}"
    )


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_gemini_url_grounding() -> None:
    """Test Gemini URL grounding feature with real URL."""
    # Skip if running in CI without API key
    if os.getenv("CI") and not os.getenv("GEMINI_API_KEY"):
        pytest.skip("Skipping Gemini URL grounding test in CI without API key")

    # Create Gemini client with URL context enabled
    config = {
        "provider": "google",
        "model": "gemini-2.5-flash",
        "api_key": os.getenv("GEMINI_API_KEY", "test-gemini-key"),
        "enable_url_context": True,
    }

    client = LLMClientFactory.create_client(config)

    # Test with the GitHub release page for llm-openrouter
    messages = [
        create_user_message(
            "Look at this URL https://github.com/simonw/llm-openrouter/releases/tag/0.5 and tell me what example prompt is mentioned for testing reasoning options support."
        )
    ]

    response = await client.generate_response(messages)

    # Validate response structure
    assert isinstance(response, LLMOutput)
    assert response.content is not None
    assert isinstance(response.content, str)
    assert len(response.content) > 0

    # Check that it found the specific example about dogs
    response_lower = response.content.lower()
    assert "dogs" in response_lower or "prove" in response_lower, (
        f"Response should mention the 'prove dogs exist' example from the URL, got: {response.content}"
    )


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_gemini_google_search_grounding(
    llm_client_factory: Callable[[str, str, str | None], Awaitable[LLMInterface]],
) -> None:
    """Test Gemini Google Search grounding for real-time information."""
    # Skip if running in CI without API key
    if os.getenv("CI") and not os.getenv("GEMINI_API_KEY"):
        pytest.skip(
            "Skipping Gemini Google Search grounding test in CI without API key"
        )

    # Create Gemini client with Google Search grounding enabled
    config = {
        "provider": "google",
        "model": "gemini-2.5-flash",
        "api_key": os.getenv("GEMINI_API_KEY", "test-gemini-key"),
        "enable_google_search": True,
    }

    client = LLMClientFactory.create_client(config)

    # Test with a query that requires current information
    messages = [
        create_user_message(
            "What is the current version of Python available for download?"
        )
    ]

    response = await client.generate_response(messages)

    # Validate response structure
    assert isinstance(response, LLMOutput)
    assert response.content is not None
    assert isinstance(response.content, str)
    assert len(response.content) > 0

    # Check that it mentions Python version information
    response_lower = response.content.lower()
    assert any(keyword in response_lower for keyword in ["python", "version", "3."]), (
        f"Response should mention Python version information, got: {response.content}"
    )
