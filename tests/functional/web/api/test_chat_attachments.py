"""Tests for chat API endpoints with attachment support."""

import base64
import io
import itertools
import json
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient, Response
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm import (
    AssistantMessage,
    LLMOutput,
    ToolCallFunction,
    ToolCallItem,
    UserMessage,
)
from family_assistant.llm.messages import (
    ImageUrlContentPart,
    MessageAttachmentMetadata,
)
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database
from tests.functional.web.conftest import run_chat_turn_stream
from tests.mocks.mock_llm import (
    MatcherArgs,
    RuleBasedMockLLMClient,
    extract_text_from_content,
    get_message_content,
)


def _png_bytes(color: str, size: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), color=color).save(buffer, format="PNG")
    return buffer.getvalue()


def _images_sent_to_model(args: MatcherArgs) -> set[tuple[str, bytes]]:
    """Every inline image the model was given, as (data URI header, decoded bytes)."""
    images: set[tuple[str, bytes]] = set()
    for msg in args["messages"]:
        content = get_message_content(msg)
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, ImageUrlContentPart):
                header, _, data = part.image_url["url"].partition(",")
                images.add((header, base64.b64decode(data)))
    return images


def _streamed_text(response: Response) -> str:
    return "".join(
        json.loads(data_line.removeprefix("data: "))["content"]
        for event_line, data_line in itertools.pairwise(response.text.splitlines())
        if event_line == "event: text"
    )


def _turn_end_statuses(response: Response) -> list[str]:
    return [
        json.loads(data_line.removeprefix("data: "))["status"]
        for event_line, data_line in itertools.pairwise(response.text.splitlines())
        if event_line == "event: turn_ended"
    ]


@pytest.mark.asyncio
async def test_chat_api_with_image_attachment(
    api_test_client: AsyncClient, api_mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """An uploaded image reaches the model inline, byte for byte."""
    image_bytes = _png_bytes("blue", 100)

    api_mock_llm_client.rules = [
        (
            lambda args: (
                _images_sent_to_model(args) == {("data:image/png;base64", image_bytes)}
            ),
            LLMOutput(
                content="I can see an image in your message! It appears to be a test image."
            ),
        )
    ]

    api_mock_llm_client.default_response = LLMOutput(
        content="I received your message but no image was detected."
    )

    image_data = base64.b64encode(image_bytes).decode("utf-8")
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
    response = await run_chat_turn_stream(api_test_client, payload)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert _streamed_text(response) == (
        "I can see an image in your message! It appears to be a test image."
    )


@pytest.mark.asyncio
async def test_chat_api_forwards_a_large_image_under_the_media_limit_in_full(
    api_test_client: AsyncClient, api_mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """A 15MB image is accepted, reaches the model untruncated, and the turn completes."""
    large_data = b"x" * (15 * 1024 * 1024)

    api_mock_llm_client.rules = [
        (
            lambda args: (
                _images_sent_to_model(args) == {("data:image/png;base64", large_data)}
            ),
            LLMOutput(content="Received the large image."),
        )
    ]
    api_mock_llm_client.default_response = LLMOutput(content="Default response")

    large_base64 = base64.b64encode(large_data).decode()
    base64_url = f"data:image/png;base64,{large_base64}"

    payload = {
        "prompt": "Analyze this large image",
        "attachments": [
            {"type": "image", "content": base64_url, "name": "large_image.png"}
        ],
    }

    response = await run_chat_turn_stream(api_test_client, payload)

    assert response.status_code == 200
    assert _turn_end_statuses(response) == ["complete"]
    assert _streamed_text(response) == "Received the large image."


@pytest.mark.asyncio
async def test_chat_api_multiple_attachments(
    api_test_client: AsyncClient, api_mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Every image in a multi-attachment turn reaches the model."""
    red_png = _png_bytes("red", 50)
    green_png = _png_bytes("green", 50)

    api_mock_llm_client.rules = [
        (
            lambda args: (
                _images_sent_to_model(args)
                == {
                    ("data:image/png;base64", red_png),
                    ("data:image/png;base64", green_png),
                }
            ),
            LLMOutput(content="I can see multiple images in your message!"),
        )
    ]
    api_mock_llm_client.default_response = LLMOutput(
        content="Some of the images were missing."
    )

    attachments = [
        {
            "type": "image",
            "content": f"data:image/png;base64,{base64.b64encode(png).decode()}",
            "name": f"test_image_{i}.png",
        }
        for i, png in enumerate([red_png, green_png])
    ]

    payload = {"prompt": "Compare these images", "attachments": attachments}

    response = await run_chat_turn_stream(api_test_client, payload)

    assert response.status_code == 200
    assert _streamed_text(response) == "I can see multiple images in your message!"


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
    response = await run_chat_turn_stream(api_test_client, payload)
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

    response = await run_chat_turn_stream(api_test_client, payload)
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_chat_api_rejects_other_user_attachment_reference(
    api_test_client: AsyncClient,
    attachment_registry_fixture: AttachmentRegistry,
    db_engine: AsyncEngine,
) -> None:
    db_context = Database(engine=db_engine)
    attachment = await attachment_registry_fixture.register_user_attachment(
        db_context=db_context,
        content=b"not an image",
        filename="private.txt",
        mime_type="text/plain",
        conversation_id="other-conversation",
        user_id="other_user",
    )

    response = await run_chat_turn_stream(
        api_test_client,
        {
            "prompt": "Use this attachment",
            "conversation_id": "current-conversation",
            "attachments": [
                {
                    "type": "image",
                    "content": f"/api/attachments/{attachment.attachment_id}",
                    "name": "private.txt",
                }
            ],
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Attachment not found"


@pytest.mark.asyncio
async def test_chat_api_accepts_native_ios_uploaded_attachment_reference_and_loads_history(
    api_test_client: AsyncClient,
    api_mock_llm_client: RuleBasedMockLLMClient,
    attachment_registry_fixture: AttachmentRegistry,
    db_engine: AsyncEngine,
) -> None:
    conversation_id = "web_conv_ios_attachment"
    db_context = Database(engine=db_engine)
    attachment = await attachment_registry_fixture.register_user_attachment(
        db_context=db_context,
        content=b"family trip notes",
        filename="trip.md",
        mime_type="text/markdown",
        conversation_id=None,
        user_id="test_user",
    )

    def uploaded_markdown_matcher(args: dict) -> bool:
        messages = args.get("messages", [])
        for msg in messages:
            content = get_message_content(msg)
            text = extract_text_from_content(content)
            if (
                attachment.attachment_id in text
                and "text/markdown" in text
                and "User uploaded: trip.md" in text
            ):
                return True
        return False

    api_mock_llm_client.rules = [
        (
            uploaded_markdown_matcher,
            LLMOutput(content="I received the uploaded markdown document."),
        )
    ]
    api_mock_llm_client.default_response = LLMOutput(
        content="Attachment injection was missing."
    )

    response = await run_chat_turn_stream(
        api_test_client,
        {
            "prompt": "Use this attachment",
            "conversation_id": conversation_id,
            "profile_id": "default_assistant",
            "interface_type": "web",
            "attachments": [
                {
                    "type": "document",
                    "content": f"/api/attachments/{attachment.attachment_id}",
                    "name": "trip.md",
                }
            ],
        },
    )

    assert response.status_code == 200
    assert _streamed_text(response) == "I received the uploaded markdown document."

    history_response = await api_test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/messages"
    )
    assert history_response.status_code == 200
    messages = history_response.json()["messages"]
    user_message = next(message for message in messages if message["role"] == "user")
    assert user_message["attachments"][0]["attachment_id"] == attachment.attachment_id
    assert user_message["attachments"][0]["content_url"] == (
        f"/api/attachments/{attachment.attachment_id}"
    )


@pytest.mark.asyncio
async def test_chat_api_passes_through_an_attachment_of_an_unrecognised_type(
    api_test_client: AsyncClient,
    api_mock_llm_client: RuleBasedMockLLMClient,
    attachment_registry_fixture: AttachmentRegistry,
    db_engine: AsyncEngine,
) -> None:
    """A type the client has no case for still reaches the model.

    A client labels what it recognises and falls back to something generic for
    the rest. Gating on that label meant the upload succeeded, the turn ran
    without the file and the model answered as though nothing was attached --
    a silent drop the user could only see as the assistant ignoring them.
    """
    conversation_id = "web_conv_unrecognised_type"
    db_context = Database(engine=db_engine)
    attachment = await attachment_registry_fixture.register_user_attachment(
        db_context=db_context,
        content=b"solid bracket\n",
        filename="bracket.stl",
        mime_type="model/stl",
        conversation_id=None,
        user_id="test_user",
    )

    def model_file_matcher(args: dict) -> bool:
        return any(
            attachment.attachment_id
            in extract_text_from_content(get_message_content(msg))
            for msg in args.get("messages", [])
        )

    api_mock_llm_client.rules = [
        (model_file_matcher, LLMOutput(content="I can see the 3D model."))
    ]
    api_mock_llm_client.default_response = LLMOutput(
        content="Attachment injection was missing."
    )

    response = await run_chat_turn_stream(
        api_test_client,
        {
            "prompt": "What is this?",
            "conversation_id": conversation_id,
            "profile_id": "default_assistant",
            "interface_type": "web",
            "attachments": [
                {
                    "type": "file",
                    "content": f"/api/attachments/{attachment.attachment_id}",
                    "name": "bracket.stl",
                }
            ],
        },
    )

    assert response.status_code == 200
    assert _streamed_text(response) == "I can see the 3D model."


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

    response = await run_chat_turn_stream(api_test_client, payload)

    assert response.status_code == 200
    assert _streamed_text(response) == "Hello! How can I help you?"


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

    response = await run_chat_turn_stream(api_test_client, payload)

    assert response.status_code == 200
    assert _streamed_text(response) == "Response without attachments"


@pytest.mark.asyncio
async def test_chat_api_null_attachments(
    api_test_client: AsyncClient, api_mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test API handles null attachments field."""

    api_mock_llm_client.default_response = LLMOutput(
        content="Response with null attachments"
    )

    payload = {"prompt": "Test message", "attachments": None}

    response = await run_chat_turn_stream(api_test_client, payload)

    assert response.status_code == 200
    assert _streamed_text(response) == "Response with null attachments"


@pytest.mark.asyncio
async def test_tool_result_attachments_include_complete_metadata(
    api_test_client: AsyncClient, api_mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test that tool result attachments include attachment_id and content_url in all contexts.

    This test verifies the fix for auto-attached attachments not appearing in the web UI.
    The issue was that tool messages were missing attachment_id and content_url in their
    metadata, preventing the frontend from synthesizing attach_to_response tool calls.
    """

    # Configure mock LLM to call generate_image tool
    def call_generate_image_matcher(args: dict) -> bool:
        messages = args.get("messages", [])
        # Match only when there's a user message requesting image generation
        # and no tool messages yet (to avoid infinite loops)
        has_user_request = False
        has_tool_result = False

        for msg in messages:
            if msg.role == "user" and "image" in str(msg.content or "").lower():
                has_user_request = True
            if msg.role == "tool":
                has_tool_result = True

        return has_user_request and not has_tool_result

    api_mock_llm_client.rules = [
        (
            call_generate_image_matcher,
            LLMOutput(
                content="Here's your image!",
                tool_calls=[
                    ToolCallItem(
                        id="call_test_generate_image",
                        type="function",
                        function=ToolCallFunction(
                            name="generate_image",
                            arguments=json.dumps({"prompt": "A test image"}),
                        ),
                    )
                ],
            ),
        )
    ]

    # After tool execution, just return a final response
    api_mock_llm_client.default_response = LLMOutput(content="Here's your image!")

    conversation_id = "test_conv_tool_attachment_metadata"

    # Send request to streaming endpoint
    payload = {
        "prompt": "Generate an image for me",
        "conversation_id": conversation_id,
        "interface_type": "web",
    }

    response = await run_chat_turn_stream(api_test_client, payload)

    assert response.status_code == 200

    # Parse streaming response to find tool_result event
    content = response.content.decode("utf-8")
    lines = content.strip().split("\n")

    tool_result_found = False
    attachment_metadata = None

    for i, line in enumerate(lines):
        if (
            line == "event: tool_result"
            and i + 1 < len(lines)
            and lines[i + 1].startswith("data: ")
        ):
            # Next line should be data
            try:
                data = json.loads(lines[i + 1][6:])  # Remove 'data: ' prefix
                if data.get("attachments"):
                    tool_result_found = True
                    attachment_metadata = data["attachments"][0]
                    break
            except json.JSONDecodeError:
                continue

    # Verify streaming response includes complete attachment metadata
    assert tool_result_found, (
        "Tool result with attachment not found in streaming response"
    )
    assert attachment_metadata is not None
    assert "attachment_id" in attachment_metadata, (
        "Missing attachment_id in streaming response"
    )
    assert "content_url" in attachment_metadata, (
        "Missing content_url in streaming response"
    )
    assert attachment_metadata["attachment_id"], "attachment_id is empty"
    assert attachment_metadata["content_url"], "content_url is empty"
    assert attachment_metadata["type"] == "tool_result"
    assert attachment_metadata["mime_type"] == "image/png"

    # Now verify the conversation history also includes complete metadata
    history_response = await api_test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/messages"
    )

    assert history_response.status_code == 200
    history_data = history_response.json()

    # Find the tool message in history
    tool_messages = [msg for msg in history_data["messages"] if msg["role"] == "tool"]

    assert len(tool_messages) == 1, "Expected exactly one tool message in history"
    tool_message = tool_messages[0]

    # Verify history includes complete attachment metadata
    assert tool_message["attachments"] is not None
    assert len(tool_message["attachments"]) == 1

    history_attachment = tool_message["attachments"][0]
    assert "attachment_id" in history_attachment, "Missing attachment_id in history"
    assert "content_url" in history_attachment, "Missing content_url in history"
    assert history_attachment["attachment_id"] == attachment_metadata["attachment_id"]
    assert history_attachment["content_url"] == attachment_metadata["content_url"]
    assert history_attachment["type"] == "tool_result"
    assert history_attachment["mime_type"] == "image/png"


@pytest.mark.asyncio
async def test_tool_produced_attachment_is_not_repeated_on_the_assistant_row(
    api_test_client: AsyncClient, api_mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """A tool's own attachment stays on the tool row only.

    Both rows are visible to clients, so recording the response attachment on the
    closing assistant row as well would render the same image twice.
    """

    def call_generate_image_matcher(args: dict) -> bool:
        messages = args.get("messages", [])
        has_user_request = any(
            msg.role == "user" and "image" in str(msg.content or "").lower()
            for msg in messages
        )
        has_tool_result = any(msg.role == "tool" for msg in messages)
        return has_user_request and not has_tool_result

    api_mock_llm_client.rules = [
        (
            call_generate_image_matcher,
            LLMOutput(
                content="Working on it.",
                tool_calls=[
                    ToolCallItem(
                        id="call_generate_image_dedupe",
                        type="function",
                        function=ToolCallFunction(
                            name="generate_image",
                            arguments=json.dumps({"prompt": "A test image"}),
                        ),
                    )
                ],
            ),
        )
    ]
    api_mock_llm_client.default_response = LLMOutput(content="Here's your image!")

    conversation_id = "web_conv_tool_attachment_not_repeated"
    response = await run_chat_turn_stream(
        api_test_client,
        {
            "prompt": "Generate an image for me",
            "conversation_id": conversation_id,
            "interface_type": "web",
        },
    )
    assert response.status_code == 200

    history_response = await api_test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/messages"
    )
    assert history_response.status_code == 200
    messages = history_response.json()["messages"]

    tool_attachment_ids = {
        attachment["attachment_id"]
        for message in messages
        if message["role"] == "tool"
        for attachment in message["attachments"] or []
    }
    assert len(tool_attachment_ids) == 1

    assistant_attachment_ids = {
        attachment["attachment_id"]
        for message in messages
        if message["role"] == "assistant"
        for attachment in message["attachments"] or []
    }
    assert assistant_attachment_ids == set()


@pytest.mark.asyncio
async def test_explicitly_attached_response_attachment_is_persisted(
    api_test_client: AsyncClient,
    api_mock_llm_client: RuleBasedMockLLMClient,
    attachment_registry_fixture: AttachmentRegistry,
    db_engine: AsyncEngine,
) -> None:
    """attach_to_response on an existing attachment survives into history.

    Nothing else records it: the id reaches the client only as a live stream
    event, and no tool row carries the attachment, so without this the reply's
    attachment vanishes as soon as the conversation is reloaded.
    """
    conversation_id = "web_conv_explicit_attachment_persisted"
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color="red").save(buffer, format="PNG")

    db_context = Database(engine=db_engine)
    attachment = await attachment_registry_fixture.register_user_attachment(
        db_context=db_context,
        content=buffer.getvalue(),
        filename="earlier-upload.png",
        mime_type="image/png",
        conversation_id=conversation_id,
        user_id="test_user",
    )

    def call_attach_to_response_matcher(args: dict) -> bool:
        messages = args.get("messages", [])
        has_user_request = any(msg.role == "user" for msg in messages)
        has_tool_result = any(msg.role == "tool" for msg in messages)
        return has_user_request and not has_tool_result

    api_mock_llm_client.rules = [
        (
            call_attach_to_response_matcher,
            LLMOutput(
                content="Sending it back.",
                tool_calls=[
                    ToolCallItem(
                        id="call_attach_existing",
                        type="function",
                        function=ToolCallFunction(
                            name="attach_to_response",
                            arguments=json.dumps({
                                "attachment_ids": [attachment.attachment_id]
                            }),
                        ),
                    )
                ],
            ),
        )
    ]
    api_mock_llm_client.default_response = LLMOutput(content="Here it is again.")

    response = await run_chat_turn_stream(
        api_test_client,
        {
            "prompt": "Send me that image again",
            "conversation_id": conversation_id,
            "interface_type": "web",
        },
    )
    assert response.status_code == 200

    history_response = await api_test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/messages"
    )
    assert history_response.status_code == 200
    messages = history_response.json()["messages"]

    rows_with_attachment = [
        message
        for message in messages
        if message["role"] == "assistant"
        and any(
            att["attachment_id"] == attachment.attachment_id
            for att in message["attachments"] or []
        )
    ]
    assert len(rows_with_attachment) == 1
    persisted = rows_with_attachment[0]["attachments"][0]
    assert persisted["type"] == "attachment_reference"
    # Only the reference is stored; the read path resolves the rest, which is what
    # lets a client know this is an image it should render inline.
    assert persisted["mime_type"] == "image/png"
    assert persisted["content_url"] == f"/api/attachments/{attachment.attachment_id}"


@pytest.mark.asyncio
async def test_response_attachment_is_recorded_once_when_more_tools_follow(
    api_test_client: AsyncClient,
    api_mock_llm_client: RuleBasedMockLLMClient,
    attachment_registry_fixture: AttachmentRegistry,
    db_engine: AsyncEngine,
) -> None:
    """Only the assistant row that ends the turn records the reference.

    Every iteration's `done` event repeats the turn's pending attachment ids, so
    an attach_to_response followed by a further tool call offers two assistant
    rows to record against -- and recording both would render the image twice.
    """
    conversation_id = "web_conv_attachment_recorded_once"
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color="blue").save(buffer, format="PNG")

    db_context = Database(engine=db_engine)
    attachment = await attachment_registry_fixture.register_user_attachment(
        db_context=db_context,
        content=buffer.getvalue(),
        filename="earlier-upload.png",
        mime_type="image/png",
        conversation_id=conversation_id,
        user_id="test_user",
    )

    def tool_round(args: dict, name: str) -> bool:
        """Match the round whose history ends just before `name` should be called."""
        messages = args.get("messages", [])
        tool_names = [msg.name for msg in messages if msg.role == "tool"]
        if name == "attach_to_response":
            return not tool_names
        return tool_names == ["attach_to_response"]

    api_mock_llm_client.rules = [
        (
            lambda args: tool_round(args, "attach_to_response"),
            LLMOutput(
                content="Attaching it.",
                tool_calls=[
                    ToolCallItem(
                        id="call_attach_then_more",
                        type="function",
                        function=ToolCallFunction(
                            name="attach_to_response",
                            arguments=json.dumps({
                                "attachment_ids": [attachment.attachment_id]
                            }),
                        ),
                    )
                ],
            ),
        ),
        (
            lambda args: tool_round(args, "list_notes"),
            LLMOutput(
                content="Checking your notes too.",
                tool_calls=[
                    ToolCallItem(
                        id="call_list_notes_after_attach",
                        type="function",
                        function=ToolCallFunction(
                            name="list_notes",
                            arguments="{}",
                        ),
                    )
                ],
            ),
        ),
    ]
    api_mock_llm_client.default_response = LLMOutput(content="Here it is.")

    response = await run_chat_turn_stream(
        api_test_client,
        {
            "prompt": "Send that image and check my notes",
            "conversation_id": conversation_id,
            "interface_type": "web",
        },
    )
    assert response.status_code == 200

    history_response = await api_test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/messages"
    )
    assert history_response.status_code == 200
    messages = history_response.json()["messages"]

    rows_with_attachment = [
        message
        for message in messages
        if message["role"] == "assistant"
        and any(
            att["attachment_id"] == attachment.attachment_id
            for att in message["attachments"] or []
        )
    ]
    assert len(rows_with_attachment) == 1
    # The row that ends the turn, not the one that went on to call another tool.
    assert rows_with_attachment[0]["tool_calls"] is None


@pytest.mark.asyncio
async def test_messages_returns_latest_user_profile_only_when_requested(
    api_test_client: AsyncClient, api_mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """include_conversation_profile gates the conversation-profile lookup.

    The frequent active-turn poll fetches /messages too, so the conversation
    profile is resolved only when the client explicitly asks for it on open.
    """
    api_mock_llm_client.default_response = LLMOutput(content="Done.")
    conversation_id = "web_conv_profile_adopt_endpoint"

    response = await run_chat_turn_stream(
        api_test_client,
        {
            "prompt": "Plan the trip",
            "conversation_id": conversation_id,
            "interface_type": "web",
        },
    )
    assert response.status_code == 200

    # Without the flag the field is omitted, keeping the poll payload cheap.
    default_response = await api_test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/messages"
    )
    assert default_response.status_code == 200
    assert default_response.json()["latest_user_profile_id"] is None

    # With the flag the endpoint resolves the profile the conversation's most
    # recent user message was sent under, matching that message's own tag.
    adopt_response = await api_test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/messages",
        params={"include_conversation_profile": "true"},
    )
    assert adopt_response.status_code == 200
    adopt_data = adopt_response.json()
    user_message = next(
        message for message in adopt_data["messages"] if message["role"] == "user"
    )
    assert user_message["processing_profile_id"] is not None
    assert adopt_data["latest_user_profile_id"] == user_message["processing_profile_id"]


@pytest.mark.asyncio
async def test_conversation_history_enriches_bare_attachment_references(
    api_test_client: AsyncClient,
    attachment_registry_fixture: AttachmentRegistry,
    db_engine: AsyncEngine,
) -> None:
    """A delivered reply records only an attachment id; the read path fills the rest.

    Chat interfaces persist response attachments as bare ``attachment_reference``
    entries, so without enrichment a client cannot tell an image from any other
    file (nor where to load it from) and falls back to showing a paperclip.
    """
    conversation_id = "web_conv_reference_enrichment"
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color="green").save(buffer, format="PNG")
    image_bytes = buffer.getvalue()

    db_context = Database(engine=db_engine)
    attachment = await attachment_registry_fixture.register_user_attachment(
        db_context=db_context,
        content=image_bytes,
        filename="chart.png",
        mime_type="image/png",
        conversation_id=conversation_id,
        user_id="test_user",
    )
    await db_context.message_history.add_message(
        UserMessage.from_trusted_user(content="Show me the chart"),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        user_id="test_user",
    )
    await db_context.message_history.add_message(
        AssistantMessage(content="Here it is."),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=datetime(2026, 7, 1, 12, 0, 1, tzinfo=UTC),
        attachments=[
            MessageAttachmentMetadata(
                type="attachment_reference",
                attachment_id=attachment.attachment_id,
            ),
            MessageAttachmentMetadata(
                type="attachment_reference",
                attachment_id="unknown-attachment",
            ),
        ],
    )

    response = await api_test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/messages"
    )
    assert response.status_code == 200
    assistant_message = next(
        message
        for message in response.json()["messages"]
        if message["role"] == "assistant"
    )
    resolved, unresolved = assistant_message["attachments"]

    assert resolved["attachment_id"] == attachment.attachment_id
    assert resolved["mime_type"] == "image/png"
    assert resolved["content_url"] == f"/api/attachments/{attachment.attachment_id}"
    assert resolved["url"] == resolved["content_url"]
    assert resolved["description"]
    assert resolved["size"] == len(image_bytes)

    # An id this caller cannot resolve is passed through as stored, not dropped.
    assert unresolved["attachment_id"] == "unknown-attachment"
    assert unresolved.get("mime_type") is None
    assert unresolved.get("content_url") is None
