"""Functional test for attachment ID injection in tool responses.

This test verifies that when a tool returns an attachment, the attachment ID
is properly injected into the tool response message so the LLM can reference it
in subsequent tool calls.
"""

import io
import json
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import LLMMessage, ToolMessage, UserMessage
from family_assistant.plugins.home_assistant.instance import HomeAssistantInstance
from family_assistant.plugins.runtime import ProfilePlugins
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database
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
    last_real_message,
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
        timezone=ZoneInfo(TEST_TIMEZONE_STR),
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
    )

    return ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        context_providers=[],
        service_config=service_config,
        server_url=None,
        app_config=AppConfig(),
        credential_resolvers=None,
        api_backend=None,
    )


ATTACHMENT_ID_MARKER = re.compile(r"\[Attachment ID\(s\): ([a-f0-9-]+)\]")


def last_tool_result(messages: list[LLMMessage], tool_call_id: str) -> str | None:
    """Content of the newest real message if it is the result of *tool_call_id*."""
    last_message = last_real_message(messages)
    if (
        isinstance(last_message, ToolMessage)
        and last_message.tool_call_id == tool_call_id
    ):
        return last_message.content
    return None


@pytest.mark.asyncio
async def test_attachment_id_injected_and_referenceable(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """
    Test that attachment IDs are injected into tool responses and can be referenced.

    Flow:
    1. User asks for a camera snapshot
    2. LLM calls get_camera_snapshot
    3. Tool returns image attachment with UUID
    4. LLM receives tool response with [Attachment ID(s): uuid] in content
    5. LLM calls highlight_image with that UUID
    6. highlight_image successfully uses the UUID to reference the image
    """
    camera_entity_id = "camera.test_camera"
    test_image_data = create_test_image()
    captured_attachment_id: str | None = None

    mock_ha_client = MagicMock()
    mock_ha_client.async_get_camera_snapshot = AsyncMock(return_value=test_image_data)

    tool_call_id_snapshot = f"call_snapshot_{uuid.uuid4()}"
    tool_call_id_highlight = f"call_highlight_{uuid.uuid4()}"

    def camera_snapshot_matcher(kwargs: MatcherArgs) -> bool:
        last_message = last_real_message(kwargs.get("messages", []))
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return (
            isinstance(last_message, UserMessage)
            and "camera" in last_text
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

    def highlight_matcher(kwargs: MatcherArgs) -> bool:
        """Match only once the snapshot result carrying an attachment ID arrives."""
        nonlocal captured_attachment_id
        content = last_tool_result(kwargs.get("messages", []), tool_call_id_snapshot)
        if content is None or kwargs.get("tools") is None:
            return False
        match = ATTACHMENT_ID_MARKER.search(content)
        if not match:
            return False
        captured_attachment_id = match.group(1)
        return True

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

    def final_response_matcher(kwargs: MatcherArgs) -> bool:
        """Match only once highlight_image has reported success."""
        content = last_tool_result(kwargs.get("messages", []), tool_call_id_highlight)
        return content is not None and "Successfully highlighted" in content

    final_llm_response = MockLLMOutput(
        content="I've highlighted the eagle statue in red on the camera image.",
        tool_calls=None,
    )

    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[
            (camera_snapshot_matcher, camera_snapshot_response),
            (highlight_matcher, create_highlight_response),
            (final_response_matcher, final_llm_response),
        ]
    )

    processing_service = await create_processing_service_with_image_tools(
        llm_client, "test_attachment_id_profile"
    )
    processing_service.plugins = ProfilePlugins((
        HomeAssistantInstance(mock_ha_client),
    ))

    attachment_registry = AttachmentRegistry(
        storage_path=str(tmp_path), db_engine=db_engine, config=None
    )
    processing_service.attachment_registry = attachment_registry

    user_message = "Get a camera snapshot and highlight the eagle statue on it"
    db_context = Database(engine=db_engine)
    result = await processing_service.handle_chat_interaction(
        db_context=db_context,
        chat_interface=MagicMock(),
        interface_type="test",
        conversation_id=TEST_CHAT_ID,
        trigger_content_parts=[{"type": "text", "text": user_message}],
        trigger_interface_message_id="msg_attachment_id_test",
        user_name=TEST_USER_NAME,
    )

    assert result.error_traceback is None, (
        f"Error during interaction: {result.error_traceback}"
    )
    assert captured_attachment_id is not None, (
        "Attachment ID was not captured from tool response"
    )
    assert result.text_reply == final_llm_response.content

    snapshot_metadata = await attachment_registry.get_attachment(
        db_context, captured_attachment_id, acting_user_id=None
    )
    assert snapshot_metadata is not None
    assert snapshot_metadata.mime_type == "image/png"

    history = await db_context.message_history.get_recent(
        interface_type="test", conversation_id=TEST_CHAT_ID
    )
    highlight_results = [
        message.content
        for message in history
        if isinstance(message, ToolMessage)
        and message.tool_call_id == tool_call_id_highlight
    ]
    assert len(highlight_results) == 1, history
    highlight_content = highlight_results[0]
    assert "Successfully highlighted" in highlight_content
    highlighted_match = ATTACHMENT_ID_MARKER.search(highlight_content)
    assert highlighted_match is not None, highlight_content
    highlighted_attachment_id = highlighted_match.group(1)
    assert highlighted_attachment_id != captured_attachment_id

    highlighted_metadata = await attachment_registry.get_attachment(
        db_context, highlighted_attachment_id, acting_user_id=None
    )
    assert highlighted_metadata is not None
    assert highlighted_metadata.mime_type == "image/png"
