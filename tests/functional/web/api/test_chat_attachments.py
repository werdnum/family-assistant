"""Tests for chat API endpoints with attachment support."""

import base64
import io
import json
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm import (
    AssistantMessage,
    LLMOutput,
    ToolCallFunction,
    ToolCallItem,
    UserMessage,
)
from family_assistant.llm.messages import MessageAttachmentMetadata
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.context import get_db_context
from tests.functional.web.conftest import run_chat_turn_stream
from tests.mocks.mock_llm import (
    RuleBasedMockLLMClient,
    extract_text_from_content,
    get_message_content,
)


@pytest.mark.asyncio
async def test_chat_api_with_image_attachment(
    api_test_client: AsyncClient, api_mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test sending image attachments via chat API."""

    # Configure mock LLM to respond to image content
    def image_matcher(args: dict) -> bool:
        messages = args.get("messages", [])
        for msg in messages:
            content = msg.content or []
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
    response = await run_chat_turn_stream(api_test_client, payload)

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
    response = await run_chat_turn_stream(api_test_client, payload)

    # Should still return 200 (validation happens on frontend)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_chat_api_multiple_attachments(
    api_test_client: AsyncClient, api_mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test sending multiple attachments via API."""

    def multi_image_matcher(args: dict) -> bool:
        # After the refactor to typed messages, user messages from history
        # only contain text content (images are stored as attachment metadata).
        # Just match any request with the specific prompt text.
        messages = args.get("messages", [])

        for msg in messages:
            content = get_message_content(msg)
            text = extract_text_from_content(content)
            if "compare these images" in text.lower():
                return True
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

    response = await run_chat_turn_stream(api_test_client, payload)

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
    async with get_db_context(engine=db_engine) as db_context:
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
    async with get_db_context(engine=db_engine) as db_context:
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
    streamed_text = ""
    for line in response.content.decode("utf-8").splitlines():
        if line.startswith("data: "):
            data = json.loads(line.removeprefix("data: "))
            streamed_text += data.get("content", "")
    assert streamed_text == "I received the uploaded markdown document."

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

    response = await run_chat_turn_stream(api_test_client, payload)

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

    response = await run_chat_turn_stream(api_test_client, payload)

    assert response.status_code == 200

    content = response.content.decode("utf-8")
    assert "Response" in content


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

    async with get_db_context(engine=db_engine) as db_context:
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

    async with get_db_context(engine=db_engine) as db_context:
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


# Note: Removed test_chat_api_trigger_content_structure as it was testing internal
# implementation details rather than API behavior. The structure of messages sent
# to the LLM is an internal concern and the actual API functionality is tested
# by the other attachment tests.


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

    async with get_db_context(engine=db_engine) as db_context:
        attachment = await attachment_registry_fixture.register_user_attachment(
            db_context=db_context,
            content=image_bytes,
            filename="chart.png",
            mime_type="image/png",
            conversation_id=conversation_id,
            user_id="test_user",
        )
        await db_context.message_history.add_message(
            UserMessage(content="Show me the chart"),
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
