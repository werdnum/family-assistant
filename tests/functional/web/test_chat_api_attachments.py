"""Tests for chat API endpoints with attachment support."""

import base64
import io
import json

import pytest
from httpx import AsyncClient
from PIL import Image

from family_assistant.llm import LLMOutput
from tests.mocks.mock_llm import RuleBasedMockLLMClient


@pytest.mark.asyncio
async def test_chat_api_with_image_attachment(
    api_test_client: AsyncClient, api_mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test sending image attachments via chat API."""

    # Configure mock LLM to respond to image content
    def image_matcher(args: dict) -> bool:
        messages = args.get("messages", [])
        for msg in messages:
            content = msg.get("content", [])
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        return True
        return False

    api_mock_llm_client.rules = [
        (
            image_matcher,
            LLMOutput(
                content="I can see an image in your message! It appears to be a test image."
            ),
        )
    ]

    api_mock_llm_client.default_response = LLMOutput(
        content="I received your message but no image was detected."
    )

    # Create a test image and convert to base64
    img = Image.new("RGB", (100, 100), color="blue")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)

    # Convert to base64 data URL
    image_data = base64.b64encode(buffer.getvalue()).decode("utf-8")
    base64_url = f"data:image/png;base64,{image_data}"

    # Prepare API request with attachment
    payload = {
        "prompt": "What do you see in this image?",
        "conversation_id": "test_conv_001",
        "profile_id": "default_assistant",
        "interface_type": "web",
        "attachments": [
            {"type": "image", "content": base64_url, "name": "test_image.png"}
        ],
    }

    # Send request to streaming endpoint
    response = await api_test_client.post(
        "/api/v1/chat/send_message_stream", json=payload
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"

    # Parse streaming response
    content = response.content.decode("utf-8")
    lines = content.strip().split("\n")

    # Look for text content in the stream
    text_content = ""
    for line in lines:
        if line.startswith("data: "):
            try:
                data = json.loads(line[6:])  # Remove 'data: ' prefix
                if "content" in data:
                    text_content += data["content"]
            except json.JSONDecodeError:
                continue

    # Verify response mentions image detection
    assert "image" in text_content.lower()


@pytest.mark.asyncio
async def test_chat_api_attachment_validation_size(
    api_test_client: AsyncClient, api_mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test attachment size validation in API."""

    api_mock_llm_client.default_response = LLMOutput(content="Default response")

    # Create a large base64 string to simulate oversized image
    large_data = "x" * (15 * 1024 * 1024)  # 15MB of data
    large_base64 = base64.b64encode(large_data.encode()).decode()
    base64_url = f"data:image/png;base64,{large_base64}"

    payload = {
        "prompt": "Analyze this large image",
        "attachments": [
            {"type": "image", "content": base64_url, "name": "large_image.png"}
        ],
    }

    # API should handle this gracefully without crashing
    response = await api_test_client.post(
        "/api/v1/chat/send_message_stream", json=payload
    )

    # Should still return 200 (validation happens on frontend)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_chat_api_multiple_attachments(
    api_test_client: AsyncClient, api_mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test sending multiple attachments via API."""

    def multi_image_matcher(args: dict) -> bool:
        messages = args.get("messages", [])
        for msg in messages:
            content = msg.get("content", [])
            if isinstance(content, list):
                image_count = sum(
                    1
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "image_url"
                )
                return image_count >= 2
        return False

    api_mock_llm_client.rules = [
        (
            multi_image_matcher,
            LLMOutput(content="I can see multiple images in your message!"),
        )
    ]

    # Create two test images
    attachments = []
    for i, color in enumerate(["red", "green"]):
        img = Image.new("RGB", (50, 50), color=color)
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)

        image_data = base64.b64encode(buffer.getvalue()).decode("utf-8")
        base64_url = f"data:image/png;base64,{image_data}"

        attachments.append({
            "type": "image",
            "content": base64_url,
            "name": f"test_image_{i}.png",
        })

    payload = {"prompt": "Compare these images", "attachments": attachments}

    response = await api_test_client.post(
        "/api/v1/chat/send_message_stream", json=payload
    )

    assert response.status_code == 200

    # Parse streaming response
    content = response.content.decode("utf-8")
    lines = content.strip().split("\n")

    text_content = ""
    for line in lines:
        if line.startswith("data: "):
            try:
                data = json.loads(line[6:])
                if "content" in data:
                    text_content += data["content"]
            except json.JSONDecodeError:
                continue

    assert "multiple" in text_content.lower() or "images" in text_content.lower()


@pytest.mark.asyncio
async def test_chat_api_attachment_format_validation(
    api_test_client: AsyncClient, api_mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test attachment format validation in API."""

    api_mock_llm_client.default_response = LLMOutput(content="Response")

    # Test with malformed attachment
    payload = {
        "prompt": "Test message",
        "attachments": [
            {
                "type": "image",
                # Missing content field
                "name": "test.png",
            }
        ],
    }

    # API should properly validate and return 400 for missing content
    response = await api_test_client.post(
        "/api/v1/chat/send_message_stream", json=payload
    )
    assert response.status_code == 400

    # Test with invalid base64 (also should return 400)
    payload = {
        "prompt": "Test message",
        "attachments": [
            {
                "type": "image",
                "content": "123",
                "name": "test.png",
            }  # Invalid base64 padding
        ],
    }

    response = await api_test_client.post(
        "/api/v1/chat/send_message_stream", json=payload
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_chat_api_no_attachments(
    api_test_client: AsyncClient, api_mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test API works normally without attachments."""

    api_mock_llm_client.default_response = LLMOutput(
        content="Hello! How can I help you?"
    )

    payload = {
        "prompt": "Hello there",
        # No attachments field
    }

    response = await api_test_client.post(
        "/api/v1/chat/send_message_stream", json=payload
    )

    assert response.status_code == 200

    # Verify normal response
    content = response.content.decode("utf-8")
    assert "Hello" in content


@pytest.mark.asyncio
async def test_chat_api_empty_attachments_array(
    api_test_client: AsyncClient, api_mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test API handles empty attachments array."""

    api_mock_llm_client.default_response = LLMOutput(
        content="Response without attachments"
    )

    payload = {
        "prompt": "Test message",
        "attachments": [],  # Empty array
    }

    response = await api_test_client.post(
        "/api/v1/chat/send_message_stream", json=payload
    )

    assert response.status_code == 200

    content = response.content.decode("utf-8")
    assert "Response" in content


@pytest.mark.asyncio
async def test_chat_api_null_attachments(
    api_test_client: AsyncClient, api_mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test API handles null attachments field."""

    api_mock_llm_client.default_response = LLMOutput(
        content="Response with null attachments"
    )

    payload = {"prompt": "Test message", "attachments": None}

    response = await api_test_client.post(
        "/api/v1/chat/send_message_stream", json=payload
    )

    assert response.status_code == 200

    content = response.content.decode("utf-8")
    assert "Response" in content


# Note: Removed test_chat_api_trigger_content_structure as it was testing internal
# implementation details rather than API behavior. The structure of messages sent
# to the LLM is an internal concern and the actual API functionality is tested
# by the other attachment tests.
