"""Integration tests for Home Assistant tools with real Home Assistant."""

import json
from zoneinfo import ZoneInfo

import homeassistant_api
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.home_assistant_wrapper import HomeAssistantClientWrapper
from family_assistant.storage.database import Database
from family_assistant.tools.home_assistant import (
    download_state_history_tool,
    get_camera_snapshot_tool,
    render_home_assistant_template_tool,
)
from family_assistant.tools.types import ToolExecutionContext


@pytest.mark.integration
@pytest.mark.vcr
async def test_render_template_tool(
    home_assistant_service: tuple[str, str | None],
    db_engine: AsyncEngine,
) -> None:
    """Test rendering a Home Assistant template through our tool with real HA."""
    base_url, token = home_assistant_service

    # Create library client
    ha_lib_client = homeassistant_api.Client(
        api_url=f"{base_url}/api",
        token=token or "test",
        use_async=True,
    )

    # Create our wrapper
    wrapper = HomeAssistantClientWrapper(
        api_url=base_url,
        token=token or "test",
        client=ha_lib_client,
    )

    # Create minimal ToolExecutionContext
    db_context = Database(engine=db_engine)
    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conversation",
        user_name="test_user",
        turn_id=None,
        db_context=db_context,
        processing_service=None,
        clock=None,
        home_assistant_client=wrapper,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    try:
        # Test rendering a simple template
        result = await render_home_assistant_template_tool(
            exec_context, template="{{ states('input_boolean.test_switch') }}"
        )

        # Verify we got a result (should be 'on' or 'off')
        assert isinstance(result, str)
        assert result in {"on", "off"}

    finally:
        await ha_lib_client.async_cache_session.close()


@pytest.mark.integration
@pytest.mark.vcr
async def test_camera_snapshot_tool_list_cameras(
    home_assistant_service: tuple[str, str | None],
    db_engine: AsyncEngine,
) -> None:
    """Test listing available cameras through our tool with real HA."""
    base_url, token = home_assistant_service

    # Create library client
    ha_lib_client = homeassistant_api.Client(
        api_url=f"{base_url}/api",
        token=token or "test",
        use_async=True,
    )

    # Create our wrapper
    wrapper = HomeAssistantClientWrapper(
        api_url=base_url,
        token=token or "test",
        client=ha_lib_client,
    )

    # Create minimal ToolExecutionContext
    db_context = Database(engine=db_engine)
    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conversation",
        user_name="test_user",
        turn_id=None,
        db_context=db_context,
        processing_service=None,
        clock=None,
        home_assistant_client=wrapper,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    try:
        # Test listing cameras (no entity_id provided)
        result = await get_camera_snapshot_tool(exec_context, camera_entity_id=None)

        # Verify we got a list of cameras
        result_text = result.get_text()
        assert "camera.demo_camera" in result_text.lower()

    finally:
        await ha_lib_client.async_cache_session.close()


@pytest.mark.integration
@pytest.mark.vcr
async def test_camera_snapshot_tool_get_snapshot(
    home_assistant_service: tuple[str, str | None],
    db_engine: AsyncEngine,
) -> None:
    """Test retrieving camera snapshot through our tool with real HA."""
    base_url, token = home_assistant_service

    # Create library client
    ha_lib_client = homeassistant_api.Client(
        api_url=f"{base_url}/api",
        token=token or "test",
        use_async=True,
    )

    # Create our wrapper
    wrapper = HomeAssistantClientWrapper(
        api_url=base_url,
        token=token or "test",
        client=ha_lib_client,
    )

    # Create minimal ToolExecutionContext
    db_context = Database(engine=db_engine)
    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conversation",
        user_name="test_user",
        turn_id=None,
        db_context=db_context,
        processing_service=None,
        clock=None,
        home_assistant_client=wrapper,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    try:
        # Test getting snapshot from demo camera
        result = await get_camera_snapshot_tool(
            exec_context, camera_entity_id="camera.demo_camera"
        )

        # Verify we got a ToolResult with attachment
        result_text = result.get_text()
        assert "camera.demo_camera" in result_text.lower()
        assert result.attachments is not None
        assert len(result.attachments) == 1

        # Verify attachment is an image
        attachment = result.attachments[0]
        assert attachment.mime_type.startswith("image/")
        assert attachment.content is not None
        assert len(attachment.content) > 0

        # Verify it looks like an image (check magic bytes)
        assert (
            attachment.content[:2] in {b"\xff\xd8", b"\x89P"}
            or attachment.content[:3] == b"GIF"
        )

    finally:
        await ha_lib_client.async_cache_session.close()


@pytest.mark.integration
@pytest.mark.vcr
async def test_history_tool_with_entities(
    home_assistant_service: tuple[str, str | None],
    db_engine: AsyncEngine,
) -> None:
    """Test downloading state history for specific entities through our tool with real HA."""
    base_url, token = home_assistant_service

    # Create library client
    ha_lib_client = homeassistant_api.Client(
        api_url=f"{base_url}/api",
        token=token or "test",
        use_async=True,
    )

    # Create our wrapper
    wrapper = HomeAssistantClientWrapper(
        api_url=base_url,
        token=token or "test",
        client=ha_lib_client,
    )

    # Create minimal ToolExecutionContext
    db_context = Database(engine=db_engine)
    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conversation",
        user_name="test_user",
        turn_id=None,
        db_context=db_context,
        processing_service=None,
        clock=None,
        home_assistant_client=wrapper,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    try:
        # Test downloading history for test entities
        result = await download_state_history_tool(
            exec_context,
            entity_ids=["input_boolean.test_switch", "input_text.test_sensor"],
            start_time=None,  # Will default to 24 hours ago
            end_time=None,  # Will default to now
            significant_changes_only=False,
        )

        # The checked-in SQLite recording has no history, while the PostgreSQL
        # recording contains both requested entities.
        if db_engine.dialect.name == "sqlite":
            assert (
                result.get_text()
                == "No history data found for the specified parameters."
            )
            assert not result.attachments
        else:
            assert "State history for 2 entities" in result.get_text()
            assert result.attachments is not None
            assert len(result.attachments) == 1
            attachment = result.attachments[0]
            assert attachment.mime_type == "application/json"
            assert attachment.content is not None

            json_data = json.loads(attachment.content.decode("utf-8"))
            assert json_data["significant_changes_only"] is False
            assert json_data["start_time"] < json_data["end_time"]
            states_by_entity = {
                entity["entity_id"]: [state["state"] for state in entity["states"]]
                for entity in json_data["entities"]
            }
            assert states_by_entity == {
                "input_boolean.test_switch": ["off"],
                "input_text.test_sensor": ["test_value"],
            }

    finally:
        await ha_lib_client.async_cache_session.close()
