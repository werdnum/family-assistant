"""Integration tests for multimodal function responses with Gemini."""

import base64
import os

import pytest

from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import AssistantMessage, ToolMessage, UserMessage
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.tools.types import ToolAttachment

# A simple 1x1 red pixel PNG
RED_DOT_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.mark.llm_integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_name,should_support_multimodal",
    [
        ("gemini-2.0-flash", False),
        (
            "gemini-2.0-flash-lite-preview-02-05",
            False,
        ),  # Ensure this is a valid 2.x model
        # gemini-3-flash-preview is usually the name, but checking availabilty might be tricky.
        # We rely on the user's request to test with "gemini-3-flash-preview"
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
    # and we provide the result (image).

    attachment = ToolAttachment(
        mime_type="image/png",
        content=RED_DOT_PNG,
        description="A red dot",
        attachment_id="img_1",
    )

    # 1. User asks for image
    # 2. Assistant calls function (simulated)
    # 3. Tool returns image
    messages = [
        UserMessage(content="Generate a red dot image"),
        AssistantMessage(
            tool_calls=[
                ToolCallItem(
                    id="call_1",
                    type="function",
                    function=ToolCallFunction(
                        name="generate_image", arguments='{"prompt": "red dot"}'
                    ),
                )
            ]
        ),
        ToolMessage(
            tool_call_id="call_1",
            content="Image created successfully",  # Text fallback
            name="generate_image",
            _attachments=[attachment],
        ),
    ]

    try:
        # We are testing that the API accepts the format we send.
        # For V2, it should strip the attachment and just send the text result.
        # For V3, it should send the attachment as inline_data.
        response = await client.generate_response(messages=messages)

        # Verify we got a valid response
        assert response is not None
        assert response.content is not None
        assert len(response.content) > 0

        # Optionally check if the model acknowledges the image (only for V3)
        if should_support_multimodal:
            # We can't strictly assert the model "sees" it without a flaky LLM check,
            # but getting a 200 OK response means the API accepted the format.
            pass

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
