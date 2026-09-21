"""Integration tests for tool image attachments with Gemini 3 models.

Regression coverage for a production bug: a tool result that carried an image
attachment was serialized as a multimodal ``FunctionResponse`` inside a legacy
``role="function"`` Content. Gemini rejects that role, so function responses are
delivered in a ``role="user"`` Content (see ``_convert_messages_to_genai_format``).
"""

import base64
import os
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import AssistantMessage, ToolMessage, UserMessage
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.tools.types import ToolAttachment

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

# A solid red 64x64 PNG, embedded so the recorded request (and thus the replay
# cassette) is byte-stable across runs. The colour is never mentioned in text,
# so the model can only answer "red" if it actually receives the image.
_RED_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAX0lEQVR4nO3PQQ0AIBDAMMC/50ME"
    "j4ZkVbDtWX87OuBVA1oDWgNaA1oDWgNaA1oDWgNaA1oDWgNaA1oDWgNaA1oDWgNaA1oDWgNaA1o"
    "DWgNaA1oDWgNaA1oDWgNaA9oFUoUBf3Xr7AgAAAAASUVORK5CYII="
)


@pytest_asyncio.fixture
async def google_client_gemini3_flash(
    request: pytest.FixtureRequest, llm_record_mode: str
) -> "AsyncGenerator[GoogleGenAIClient]":
    """A gemini-3.8-flash client wired for SDK record/replay."""
    api_key = os.getenv("GEMINI_API_KEY", "test-gemini-key")
    test_name = request.node.name
    module_name = request.node.module.__name__.replace("tests.", "")
    debug_config: dict[str, str | None] = {
        "client_mode": llm_record_mode,
        "replay_id": f"{module_name}/{test_name}/mldev",
        "replays_directory": "tests/cassettes/gemini",
    }
    client = GoogleGenAIClient(
        api_key=api_key,
        model="gemini-3.8-flash",
        debug_config=debug_config,
    )
    try:
        yield client
    finally:
        await client.close()


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.asyncio
async def test_tool_image_attachment_delivered_to_gemini3(
    google_client_gemini3_flash: GoogleGenAIClient,
) -> None:
    """A tool image attachment must reach gemini-3.8-flash without a 400.

    Sends a conversation where a tool returns a red image as an attachment. The
    colour is not stated in any text, so a correct "red" answer proves the image
    was delivered. On the buggy code this raised ``400 INVALID_ARGUMENT`` because
    the multimodal function response was emitted in a ``role="function"`` Content;
    after the fix it is emitted in a ``role="user"`` Content and the model can see it.
    """
    if os.getenv("CI") and not os.getenv("GEMINI_API_KEY"):
        pytest.skip("Skipping Google test in CI without API key")

    tool_msg = ToolMessage(
        tool_call_id="call_1",
        content="Here is the image.",  # no colour hint - must come from the image
        name="get_image",
        _attachments=[
            ToolAttachment(
                mime_type="image/png",
                content=_RED_PNG,
                description="An image",
                attachment_id="img_1",
            )
        ],
    )

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
        tool_msg,
    ]

    # tool_choice="none" forces a text answer instead of another function call.
    response = await google_client_gemini3_flash.generate_response(
        messages=messages, tool_choice="none"
    )

    assert response is not None
    assert response.content is not None, (
        f"Expected text content but got None. tool_calls={response.tool_calls!r}"
    )
    assert "red" in response.content.lower(), (
        f"Expected the model to mention 'red' but got: {response.content!r}"
    )
