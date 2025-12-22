"""Integration tests for multimodal function responses with Gemini."""

from unittest.mock import MagicMock, patch

import pytest

from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import AssistantMessage, ToolMessage, UserMessage
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.tools.types import ToolAttachment


@pytest.mark.llm_integration
@pytest.mark.asyncio
async def test_multimodal_function_response_integration() -> None:
    """
    Test that the Gemini client correctly formats multimodal responses for V3 models
    and standard responses for V2 models.

    This integration test verifies the full flow from client initialization
    to message formatting, without hitting the actual API (using mocks for the network layer).
    """

    # 1. Setup a mock for the genai client to capture what's passed to it
    with patch(
        "family_assistant.llm.providers.google_genai_client.genai.Client"
    ) as MockClient:
        # Create a mock instance
        mock_instance = MagicMock()
        MockClient.return_value = mock_instance

        # Setup async return value for generate_content
        mock_response = MagicMock()
        mock_response.text = "Response received"
        mock_response.candidates = []

        # Configure the async mock correctly
        async_mock = MagicMock()
        async_mock.models.generate_content.return_value = mock_response
        mock_instance.aio = async_mock

        # ==================================================================================
        # SCENARIO 1: Gemini 3 (Multimodal Support)
        # ==================================================================================
        client_v3 = GoogleGenAIClient(
            api_key="test_key", model="gemini-3-flash-preview"
        )

        # Create a conversation with a tool result containing an image
        attachment = ToolAttachment(
            mime_type="image/png",
            content=b"fake_image_content",
            description="Generated Image",
            attachment_id="img_1",
        )

        messages_v3 = [
            UserMessage(content="Generate an image"),
            AssistantMessage(
                tool_calls=[
                    ToolCallItem(
                        id="call_1",
                        type="function",
                        function=ToolCallFunction(
                            name="generate_image", arguments="{}"
                        ),
                    )
                ]
            ),
            ToolMessage(
                tool_call_id="call_1",
                content="Image created",
                name="generate_image",
                _attachments=[attachment],
            ),
        ]

        # Call generate_response
        await client_v3.generate_response(messages=messages_v3)

        # Verify the call arguments
        call_args_v3 = async_mock.models.generate_content.call_args
        assert call_args_v3 is not None

        # Inspect contents passed to the API
        contents_v3 = call_args_v3.kwargs["contents"]

        # Find the function response content
        tool_content_v3 = next(c for c in contents_v3 if c.role == "function")
        assert len(tool_content_v3.parts) == 1

        part_v3 = tool_content_v3.parts[0]
        fr_v3 = part_v3.function_response

        # For Gemini 3, we expect parts with inline_data in the function response
        # Note: Depending on SDK version used in tests vs runtime, structure might vary slightly,
        # but our code puts 'parts' in kwargs for FunctionResponse.

        # Check that we have parts in the function response
        assert fr_v3.parts is not None
        assert len(fr_v3.parts) == 1
        assert fr_v3.parts[0].inline_data.data == b"fake_image_content"
        assert fr_v3.parts[0].inline_data.mime_type == "image/png"

        # ==================================================================================
        # SCENARIO 2: Gemini 2.5 (No Multimodal Support)
        # ==================================================================================
        client_v2 = GoogleGenAIClient(api_key="test_key", model="gemini-2.5-flash")

        # Call generate_response with same messages
        await client_v2.generate_response(messages=messages_v3)

        # Verify the call arguments
        call_args_v2 = async_mock.models.generate_content.call_args
        assert call_args_v2 is not None

        # Inspect contents passed to the API
        contents_v2 = call_args_v2.kwargs["contents"]

        # Find the function response content
        tool_content_v2 = next(c for c in contents_v2 if c.role == "function")
        assert len(tool_content_v2.parts) == 1

        part_v2 = tool_content_v2.parts[0]
        fr_v2 = part_v2.function_response

        # For Gemini 2.5, we expect NO parts in the function response, only standard response fields
        assert fr_v2.parts is None or len(fr_v2.parts) == 0
