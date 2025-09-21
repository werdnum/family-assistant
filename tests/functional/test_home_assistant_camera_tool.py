"""Test Home Assistant camera snapshot tool."""

import json
import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS as local_tool_implementations,
)
from family_assistant.tools import (
    TOOLS_DEFINITION as local_tools_definition,
)
from family_assistant.tools import (
    CompositeToolsProvider,
    LocalToolsProvider,
    MCPToolsProvider,
)
from family_assistant.tools.home_assistant import detect_image_mime_type

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

TEST_CHAT_ID = "ha_camera_test_123"
TEST_USER_NAME = "HACameraTestUser"
TEST_TIMEZONE_STR = "UTC"

# Mock image data - small JPEG header for testing
MOCK_JPEG_DATA = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb\x00C\x00"
)


async def create_processing_service_for_camera_tests(
    llm_client: "LLMInterface", profile_id: str
) -> ProcessingService:
    """Helper function to create a ProcessingService for camera snapshot tests."""
    dummy_prompts = {"system_prompt": "You are a helpful assistant."}

    enabled_tools = ["get_camera_snapshot"]
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
        app_config={},
    )


@pytest.mark.asyncio
async def test_get_camera_snapshot_success(
    db_engine: AsyncEngine,
) -> None:
    """
    Test successful retrieval of a camera snapshot.
    1. User asks for a camera snapshot
    2. LLM decides to use get_camera_snapshot tool
    3. Tool executes with mocked HA client returning image data
    4. LLM receives result and responds to user
    """
    camera_entity_id = "camera.front_door"

    # Create mock Home Assistant client
    mock_ha_client = MagicMock()
    mock_ha_client.async_request = AsyncMock(return_value=MOCK_JPEG_DATA)

    tool_call_id = f"call_camera_snapshot_{uuid.uuid4()}"

    # --- LLM Rules ---
    def camera_snapshot_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return (
            "camera" in last_text
            and "snapshot" in last_text
            and "front door" in last_text
            and kwargs.get("tools") is not None
        )

    camera_snapshot_response = MockLLMOutput(
        content="I'll get a snapshot from the front door camera for you.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="get_camera_snapshot",
                    arguments=json.dumps({"camera_entity_id": camera_entity_id}),
                ),
            )
        ],
    )

    def final_response_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if len(messages) < 2:
            return False
        last_message = messages[-1]
        return (
            last_message.get("role") == "tool"
            and last_message.get("tool_call_id") == tool_call_id
            and f"Retrieved snapshot from camera '{camera_entity_id}'"
            in last_message.get("content", "")
        )

    final_llm_response = MockLLMOutput(
        content="Here's the current snapshot from the front door camera.",
        tool_calls=None,
    )

    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[
            (camera_snapshot_matcher, camera_snapshot_response),
            (final_response_matcher, final_llm_response),
        ]
    )

    # --- Setup ProcessingService ---
    processing_service = await create_processing_service_for_camera_tests(
        llm_client, "test_ha_camera_profile"
    )

    # Inject the mock HA client
    processing_service.home_assistant_client = mock_ha_client

    # --- Simulate User Interaction ---
    user_message = "Can you get a snapshot from the front door camera?"
    async with DatabaseContext(engine=db_engine) as db_context:
        (
            final_reply,
            _,
            _,
            error,
        ) = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test",
            conversation_id=TEST_CHAT_ID,
            trigger_content_parts=[{"type": "text", "text": user_message}],
            trigger_interface_message_id="msg_ha_camera_test",
            user_name=TEST_USER_NAME,
        )

    assert error is None, f"Error during interaction: {error}"
    assert final_reply and "snapshot" in final_reply.lower(), (
        f"Expected 'snapshot' not in reply: '{final_reply}'"
    )


@pytest.mark.asyncio
async def test_get_camera_snapshot_list_cameras(
    db_engine: AsyncEngine,
) -> None:
    """
    Test listing available cameras when no entity_id is provided.
    """
    # Create mock Home Assistant client
    mock_ha_client = MagicMock()

    # Mock state objects
    mock_camera_states = [
        MagicMock(
            entity_id="camera.front_door",
            attributes={"friendly_name": "Front Door Camera"},
        ),
        MagicMock(
            entity_id="camera.backyard", attributes={"friendly_name": "Backyard Camera"}
        ),
        MagicMock(
            entity_id="camera.driveway",
            attributes={},  # No friendly name
        ),
        MagicMock(
            entity_id="sensor.temperature",  # Non-camera entity
            attributes={"friendly_name": "Temperature Sensor"},
        ),
    ]

    mock_ha_client.async_get_states = AsyncMock(return_value=mock_camera_states)

    tool_call_id = f"call_list_cameras_{uuid.uuid4()}"

    # --- LLM Rules ---
    def list_cameras_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return (
            "cameras" in last_text
            and "available" in last_text
            and kwargs.get("tools") is not None
        )

    list_cameras_response = MockLLMOutput(
        content="I'll list the available cameras for you.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="get_camera_snapshot",
                    arguments=json.dumps({}),  # No camera_entity_id provided
                ),
            )
        ],
    )

    def cameras_list_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if len(messages) < 2:
            return False
        last_message = messages[-1]
        content = last_message.get("content", "")
        return (
            last_message.get("role") == "tool"
            and last_message.get("tool_call_id") == tool_call_id
            and "Available cameras" in content
            and "camera.front_door" in content
            and "Front Door Camera" in content
            and "camera.backyard" in content
            and "camera.driveway" in content
        )

    cameras_list_response = MockLLMOutput(
        content="Here are the available cameras:\n"
        "- Front Door Camera (camera.front_door)\n"
        "- Backyard Camera (camera.backyard)\n"
        "- camera.driveway\n\n"
        "Which camera would you like to get a snapshot from?",
        tool_calls=None,
    )

    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[
            (list_cameras_matcher, list_cameras_response),
            (cameras_list_matcher, cameras_list_response),
        ]
    )

    # --- Setup ProcessingService ---
    processing_service = await create_processing_service_for_camera_tests(
        llm_client, "test_ha_list_cameras_profile"
    )

    # Inject the mock HA client
    processing_service.home_assistant_client = mock_ha_client

    # --- Simulate User Interaction ---
    user_message = "What cameras are available?"
    async with DatabaseContext(engine=db_engine) as db_context:
        (
            final_reply,
            _,
            _,
            error,
        ) = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test",
            conversation_id=TEST_CHAT_ID,
            trigger_content_parts=[{"type": "text", "text": user_message}],
            trigger_interface_message_id="msg_ha_list_cameras_test",
            user_name=TEST_USER_NAME,
        )

    assert error is None, f"Error during interaction: {error}"
    assert final_reply, "No reply received"
    assert "camera.front_door" in final_reply, "Front door camera not in reply"
    assert "camera.backyard" in final_reply, "Backyard camera not in reply"
    assert "camera.driveway" in final_reply, "Driveway camera not in reply"


@pytest.mark.asyncio
async def test_get_camera_snapshot_no_client(
    db_engine: AsyncEngine,
) -> None:
    """
    Test error handling when Home Assistant client is not available.
    """
    tool_call_id = f"call_camera_no_client_{uuid.uuid4()}"

    # --- LLM Rules ---
    def camera_check_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return (
            "camera" in last_text
            and "check" in last_text
            and kwargs.get("tools") is not None
        )

    camera_check_response = MockLLMOutput(
        content="I'll check the camera for you.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="get_camera_snapshot",
                    arguments=json.dumps({"camera_entity_id": "camera.test"}),
                ),
            )
        ],
    )

    def error_response_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if len(messages) < 2:
            return False
        last_message = messages[-1]
        return (
            last_message.get("role") == "tool"
            and last_message.get("tool_call_id") == tool_call_id
            and "Error:" in last_message.get("content", "")
            and "not configured" in last_message.get("content", "")
        )

    error_llm_response = MockLLMOutput(
        content="I'm sorry, but Home Assistant integration is not currently available.",
        tool_calls=None,
    )

    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[
            (camera_check_matcher, camera_check_response),
            (error_response_matcher, error_llm_response),
        ]
    )

    # --- Setup ProcessingService without HA client ---
    processing_service = await create_processing_service_for_camera_tests(
        llm_client, "test_ha_camera_no_client_profile"
    )

    # Don't set home_assistant_client - it should be None

    # --- Simulate User Interaction ---
    user_message = "Check the camera"
    async with DatabaseContext(engine=db_engine) as db_context:
        (
            final_reply,
            _,
            _,
            error,
        ) = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test",
            conversation_id=TEST_CHAT_ID,
            trigger_content_parts=[{"type": "text", "text": user_message}],
            trigger_interface_message_id="msg_ha_camera_no_client_test",
            user_name=TEST_USER_NAME,
        )

    assert error is None, f"Error during interaction: {error}"
    assert final_reply and "not currently available" in final_reply, (
        f"Expected error message not in reply: '{final_reply}'"
    )


@pytest.mark.asyncio
async def test_get_camera_snapshot_api_error(
    db_engine: AsyncEngine,
) -> None:
    """
    Test handling of Home Assistant API errors.
    """
    # Create mock Home Assistant client that raises an error
    mock_ha_client = MagicMock()
    mock_ha_client.async_request = AsyncMock(side_effect=Exception("Camera not found"))

    tool_call_id = f"call_camera_error_{uuid.uuid4()}"

    # --- LLM Rules ---
    def camera_error_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return "broken camera" in last_text and kwargs.get("tools") is not None

    camera_error_response = MockLLMOutput(
        content="I'll check the broken camera for you.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="get_camera_snapshot",
                    arguments=json.dumps({"camera_entity_id": "camera.broken"}),
                ),
            )
        ],
    )

    def api_error_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if len(messages) < 2:
            return False
        last_message = messages[-1]
        content = last_message.get("content", "")
        return (
            last_message.get("role") == "tool"
            and last_message.get("tool_call_id") == tool_call_id
            and "Error:" in content
            and "Failed to retrieve camera snapshot" in content
            and "Camera not found" in content
        )

    api_error_response = MockLLMOutput(
        content="I'm having trouble accessing that camera. "
        "It seems the camera was not found. Please check if the camera is online.",
        tool_calls=None,
    )

    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[
            (camera_error_matcher, camera_error_response),
            (api_error_matcher, api_error_response),
        ],
        default_response=MockLLMOutput(
            content="I'm having trouble accessing that camera. "
            "It seems the camera was not found. Please check if the camera is online.",
            tool_calls=None,
        ),
    )

    # --- Setup ProcessingService ---
    processing_service = await create_processing_service_for_camera_tests(
        llm_client, "test_ha_camera_api_error_profile"
    )

    # Inject the mock HA client
    processing_service.home_assistant_client = mock_ha_client

    # --- Simulate User Interaction ---
    user_message = "Check the broken camera"
    async with DatabaseContext(engine=db_engine) as db_context:
        (
            final_reply,
            _,
            _,
            error,
        ) = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test",
            conversation_id=TEST_CHAT_ID,
            trigger_content_parts=[{"type": "text", "text": user_message}],
            trigger_interface_message_id="msg_ha_camera_api_error_test",
            user_name=TEST_USER_NAME,
        )

    assert error is None, f"Error during interaction: {error}"
    assert final_reply and (
        "camera not found" in final_reply.lower()
        or "trouble accessing" in final_reply.lower()
    ), f"Expected error message not in reply: '{final_reply}'"


# Test the helper function directly
def test_detect_image_mime_type_png() -> None:
    """Test PNG detection."""
    png_header = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    assert detect_image_mime_type(png_header) == "image/png"


def test_detect_image_mime_type_jpeg() -> None:
    """Test JPEG detection."""
    jpeg_header = b"\xff\xd8\xff\xe0\x00\x10JFIF"
    assert detect_image_mime_type(jpeg_header) == "image/jpeg"


def test_detect_image_mime_type_gif87() -> None:
    """Test GIF87a detection."""
    gif87_header = b"GIF87a\x01\x00\x01\x00"
    assert detect_image_mime_type(gif87_header) == "image/gif"


def test_detect_image_mime_type_gif89() -> None:
    """Test GIF89a detection."""
    gif89_header = b"GIF89a\x01\x00\x01\x00"
    assert detect_image_mime_type(gif89_header) == "image/gif"


def test_detect_image_mime_type_webp() -> None:
    """Test WebP detection."""
    webp_header = b"RIFF\x00\x00\x00\x00WEBP"
    assert detect_image_mime_type(webp_header) == "image/webp"


def test_detect_image_mime_type_unknown() -> None:
    """Test unknown format defaults to JPEG."""
    unknown_header = b"UNKNOWN\x00\x00\x00\x00"
    assert detect_image_mime_type(unknown_header) == "image/jpeg"


def test_detect_image_mime_type_empty() -> None:
    """Test empty content defaults to JPEG."""
    assert detect_image_mime_type(b"") == "image/jpeg"
