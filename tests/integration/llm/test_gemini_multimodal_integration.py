"""Integration tests for multimodal function responses with Gemini."""

import io
import os

import pytest
from PIL import Image

from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import AssistantMessage, ToolMessage, UserMessage
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.tools.types import ToolAttachment


def create_solid_color_png(color: tuple[int, int, int], size: int = 100) -> bytes:
    """Create a solid color PNG image."""
    img = Image.new("RGB", (size, size), color)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


# A solid red square image (100x100 pixels)
RED_IMAGE_PNG = create_solid_color_png((255, 0, 0))


@pytest.mark.llm_integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_name,should_support_multimodal",
    [
        ("gemini-3-flash-preview", False),
        ("gemini-3-flash-preview", True),
    ],
)
async def test_multimodal_function_response_integration(
    model_name: str, should_support_multimodal: bool
) -> None:
    """
    Test that the Gemini client correctly formats multimodal responses for V3 models
    and standard responses for V2 models using the REAL API.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        pytest.skip("GEMINI_API_KEY not set, skipping integration test")

    client = GoogleGenAIClient(api_key=api_key, model=model_name)

    # We'll simulate a conversation where the assistant "called" a tool
    # and we provide the result (image). The color is NOT mentioned anywhere
    # in the text - the model must see the actual image to know it's red.

    attachment = ToolAttachment(
        mime_type="image/png",
        content=RED_IMAGE_PNG,
        description="An image",  # No color hint
        attachment_id="img_1",
    )

    # Conversation flow:
    # 1. User asks for an image and to describe what it shows
    # 2. Assistant calls get_image function (simulated)
    # 3. Tool returns image with attachment
    # 4. Model must describe the color by actually seeing the image
    messages = [
        UserMessage(
            content="Use the get_image tool to fetch an image, then describe what "
            "color the image shows. Be very brief - just state the color."
        ),
        AssistantMessage(
            tool_calls=[
                ToolCallItem(
                    id="call_1",
                    type="function",
                    function=ToolCallFunction(name="get_image", arguments="{}"),
                )
            ]
        ),
        ToolMessage(
            tool_call_id="call_1",
            content="Here is the image.",  # No color hint - must come from image
            name="get_image",
            _attachments=[attachment],
        ),
    ]

    try:
        # Use tool_choice="none" to force a text response instead of function calls
        response = await client.generate_response(messages=messages, tool_choice="none")

        # Verify we got a valid text response
        assert response is not None
        assert response.content is not None, (
            f"Expected text content but got None. tool_calls={response.tool_calls!r}"
        )
        assert len(response.content) > 0

        # Both V2 and V3 models should see the image and mention "red":
        # - V3 models receive it as inline_data in the FunctionResponse
        # - V2 models receive it as an injected user message with the image
        response_lower = response.content.lower()
        assert "red" in response_lower, (
            f"Expected model to mention 'red' color but got: {response.content!r}"
        )

    except Exception as e:
        # If the model doesn't exist or other API error, we might want to know.
        # But specifically we are looking for 400 Bad Request due to malformed payload.
        if "400" in str(e) or "invalid" in str(e).lower():
            pytest.fail(f"API rejected the payload for model {model_name}: {e}")
        elif "404" in str(e):
            pytest.skip(
                f"Model {model_name} not found or not available to this API key"
            )
        else:
            raise e
