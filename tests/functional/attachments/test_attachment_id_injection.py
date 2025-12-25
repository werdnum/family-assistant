"""Functional test for attachment ID injection in tool responses.

This test verifies that when a tool returns an attachment, the attachment ID
is properly injected into the tool response message so the LLM can reference it
in subsequent tool calls.
"""

import io
import json
import re
import tempfile
import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS as local_tool_implementations,
)
from family_assistant.tools import TOOLS_DEFINITION as local_tools_definition
from family_assistant.tools import (
    CompositeToolsProvider,
    LocalToolsProvider,
    MCPToolsProvider,
)

if TYPE_CHECKING:
    from family_assistant.llm import LLMInterface

from tests.mocks.mock_llm import (
    LLMOutput as MockLLMOutput,
)
from tests.mocks.mock_llm import (
    MatcherArgs,
    RuleBasedMockLLMClient,
    get_last_message_text,
)

TEST_CHAT_ID = "attachment_id_test"
TEST_USER_NAME = "AttachmentTestUser"
TEST_TIMEZONE_STR = "UTC"


def create_test_image(size: tuple[int, int] = (100, 100)) -> bytes:
    """Create a simple test image."""
    image = Image.new("RGB", size, color="blue")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def create_processing_service_with_image_tools(
    llm_client: "LLMInterface", profile_id: str
) -> ProcessingService:
    """Create a ProcessingService with image tools enabled."""
    dummy_prompts = {"system_prompt": "You are a helpful assistant."}

    enabled_tools = ["get_camera_snapshot", "highlight_image"]
    filtered_definitions = [
        tool
        for tool in local_tools_definition
        if tool.get("function", {}).get("name") in enabled_tools
    ]
    filtered_implementations = {
        name: impl
        for name, impl in local_tool_implementations.items()
        if name in enabled_tools
    }

    local_provider = LocalToolsProvider(
        definitions=filtered_definitions,
        implementations=filtered_implementations,
    )
    mcp_provider = MCPToolsProvider(mcp_server_configs={})
    composite_provider = CompositeToolsProvider(
        providers=[local_provider, mcp_provider]
    )
    await composite_provider.get_tool_definitions()

    service_config = ProcessingServiceConfig(
        id=profile_id,
        prompts=dummy_prompts,
        timezone_str=TEST_TIMEZONE_STR,
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={"confirmation_required": []},
        delegation_security_level="unrestricted",
    )

    return ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        context_providers=[],
        service_config=service_config,
        server_url=None,
        app_config=AppConfig(),
    )


@pytest.mark.asyncio
async def test_attachment_id_injected_and_referenceable(
    db_engine: AsyncEngine,
) -> None:
    """
    Test that attachment IDs are injected into tool responses and can be referenced.

    Flow:
    1. User asks for a camera snapshot
    2. LLM calls get_camera_snapshot
    3. Tool returns image attachment with UUID
    4. LLM receives tool response with [Attachment ID: uuid] in content
    5. LLM calls highlight_image with that UUID
    6. highlight_image successfully uses the UUID to reference the image
    """
    camera_entity_id = "camera.test_camera"
    test_image_data = create_test_image()
    captured_attachment_id = None

    # Mock Home Assistant client
    mock_ha_client = MagicMock()
    mock_ha_client.async_get_camera_snapshot = AsyncMock(return_value=test_image_data)

    tool_call_id_snapshot = f"call_snapshot_{uuid.uuid4()}"
    tool_call_id_highlight = f"call_highlight_{uuid.uuid4()}"

    # --- LLM Rule 1: Initial camera snapshot request ---
    def camera_snapshot_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return (
            "camera" in last_text
            and "snapshot" in last_text
            and kwargs.get("tools") is not None
        )

    camera_snapshot_response = MockLLMOutput(
        content="I'll get a snapshot from the camera.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id_snapshot,
                type="function",
                function=ToolCallFunction(
                    name="get_camera_snapshot",
                    arguments=json.dumps({"camera_entity_id": camera_entity_id}),
                ),
            )
        ],
    )

    # --- LLM Rule 2: After snapshot, highlight something ---
    def highlight_matcher(kwargs: MatcherArgs) -> bool:
        """Verify the LLM receives the tool result with attachment ID."""
        nonlocal captured_attachment_id
        messages = kwargs.get("messages", [])
        if len(messages) < 2:
            return False

        # Find the tool response message
        tool_message = None
        for msg in reversed(messages):
            if msg.role == "tool" and msg.tool_call_id == tool_call_id_snapshot:
                tool_message = msg
                break

        if not tool_message:
            return False

        # Check that the content contains the attachment ID marker (plural form)
        content = tool_message.content or ""
        if "[Attachment ID(s):" not in content:
            return False

        # Extract the attachment ID using regex (handles both singular and multiple IDs)
        match = re.search(
            r"\[Attachment ID\(s\): ([a-f0-9-]+(?:, [a-f0-9-]+)*)\]", content
        )
        if not match:
            return False

        captured_attachment_id = match.group(1)
        return True

    # This response will use the captured attachment ID
    def create_highlight_response(kwargs: MatcherArgs) -> MockLLMOutput:
        return MockLLMOutput(
            content="I'll highlight the eagle statue in the image.",
            tool_calls=[
                ToolCallItem(
                    id=tool_call_id_highlight,
                    type="function",
                    function=ToolCallFunction(
                        name="highlight_image",
                        arguments=json.dumps({
                            "image_attachment_id": captured_attachment_id,
                            "regions": [
                                {
                                    "box": [100, 100, 200, 200],
                                    "label": "eagle statue",
                                    "color": "red",
                                }
                            ],
                        }),
                    ),
                )
            ],
        )

    # --- LLM Rule 3: Final response after highlighting ---
    def final_response_matcher(kwargs: MatcherArgs) -> bool:
        """Verify the highlight tool executed successfully."""
        messages = kwargs.get("messages", [])
        # Look for the highlight tool result
        for msg in reversed(messages):
            if msg.role == "tool" and msg.tool_call_id == tool_call_id_highlight:
                content = msg.content or ""
                # Check for success message (not error)
                return (
                    "Successfully highlighted" in content
                    or "highlighted" in content.lower()
                )
        return False

    final_llm_response = MockLLMOutput(
        content="I've highlighted the eagle statue in red on the camera image.",
        tool_calls=None,
    )

    # Create the mock LLM with a callable to get the dynamic highlight response
    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[
            (camera_snapshot_matcher, camera_snapshot_response),
            (highlight_matcher, create_highlight_response),
            (final_response_matcher, final_llm_response),
        ]
    )

    # --- Setup ProcessingService ---
    processing_service = await create_processing_service_with_image_tools(
        llm_client, "test_attachment_id_profile"
    )

    # Inject the mock HA client
    processing_service.home_assistant_client = mock_ha_client

    # Create and inject AttachmentRegistry for attachment storage
    attachment_temp_dir = tempfile.mkdtemp()
    attachment_registry = AttachmentRegistry(
        storage_path=attachment_temp_dir, db_engine=db_engine, config=None
    )
    processing_service.attachment_registry = attachment_registry

    # --- Simulate User Interaction ---
    user_message = "Get a camera snapshot and highlight the eagle statue on it"
    async with DatabaseContext(engine=db_engine) as db_context:
        result = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test",
            conversation_id=TEST_CHAT_ID,
            trigger_content_parts=[{"type": "text", "text": user_message}],
            trigger_interface_message_id="msg_attachment_id_test",
            user_name=TEST_USER_NAME,
        )
        final_reply = result.text_reply
        error = result.error_traceback

    # Assertions
    assert error is None, f"Error during interaction: {error}"
    assert captured_attachment_id is not None, (
        "Attachment ID was not captured from tool response"
    )
    assert final_reply and "highlight" in final_reply.lower(), (
        f"Expected 'highlight' in reply: '{final_reply}'"
    )
