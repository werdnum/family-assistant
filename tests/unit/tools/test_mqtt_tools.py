"""Tests for MQTT publish tool."""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import aiomqtt
import pytest
from pydantic import SecretStr

from family_assistant.config_models import AppConfig, MQTTConfig
from family_assistant.storage.database import Database
from family_assistant.tools.mqtt import (
    MQTT_TOOLS_DEFINITION,
    mqtt_publish_tool,
)
from family_assistant.tools.types import ToolExecutionContext


def _make_exec_context(mqtt_config: MQTTConfig) -> ToolExecutionContext:
    mock_processing_service = MagicMock()
    mock_processing_service.app_config = AppConfig(mqtt_config=mqtt_config)
    return ToolExecutionContext(
        conversation_id="test-conv",
        user_name="test-user",
        interface_type="test",
        timezone=ZoneInfo("UTC"),
        turn_id=None,
        db_context=MagicMock(spec=Database),
        processing_service=mock_processing_service,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        credential_resolvers=None,
        api_backend=None,
    )


@pytest.fixture
def exec_context() -> ToolExecutionContext:
    return _make_exec_context(
        MQTTConfig(
            broker_host="mqtt.local",
            broker_port=1883,
            username="testuser",
            password=SecretStr("testpass"),
        )
    )


@pytest.fixture
def exec_context_no_mqtt() -> ToolExecutionContext:
    return _make_exec_context(MQTTConfig())


def test_tool_definition_structure() -> None:
    assert len(MQTT_TOOLS_DEFINITION) == 1
    tool_def = MQTT_TOOLS_DEFINITION[0]

    assert tool_def["type"] == "function"
    assert tool_def["function"]["name"] == "mqtt_publish"

    params = cast("dict[str, Any]", tool_def["function"]["parameters"])
    assert params["type"] == "object"
    assert "topic" in params["properties"]
    assert "payload" in params["properties"]
    assert "retain" in params["properties"]
    assert params["required"] == ["topic", "payload"]


@pytest.mark.asyncio
async def test_mqtt_publish(exec_context: ToolExecutionContext) -> None:
    mock_client = AsyncMock()
    with patch("family_assistant.tools.mqtt.aiomqtt.Client") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await mqtt_publish_tool(
            exec_context=exec_context,
            topic="family/eink/display",
            payload={"temperature": 22, "tasks": ["groceries"]},
        )

    data = result.get_data()
    assert isinstance(data, dict)
    assert data["topic"] == "family/eink/display"
    assert data["retain"] is True
    assert data["payload_size"] > 0

    mock_client_cls.assert_called_once_with(
        hostname="mqtt.local",
        port=1883,
        username="testuser",
        password="testpass",
    )
    mock_client.publish.assert_awaited_once()
    call_args = mock_client.publish.call_args
    assert call_args.args[0] == "family/eink/display"
    assert call_args.kwargs["retain"] is True
    assert call_args.kwargs["payload"] == b'{"temperature": 22, "tasks": ["groceries"]}'


@pytest.mark.asyncio
async def test_mqtt_publish_no_retain(exec_context: ToolExecutionContext) -> None:
    mock_client = AsyncMock()
    with patch("family_assistant.tools.mqtt.aiomqtt.Client") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await mqtt_publish_tool(
            exec_context=exec_context,
            topic="test/topic",
            payload={"key": "value"},
            retain=False,
        )

    data = result.get_data()
    assert isinstance(data, dict)
    assert data["retain"] is False

    call_args = mock_client.publish.call_args
    assert call_args.kwargs["retain"] is False


@pytest.mark.asyncio
async def test_mqtt_publish_string_payload(exec_context: ToolExecutionContext) -> None:
    mock_client = AsyncMock()
    with patch("family_assistant.tools.mqtt.aiomqtt.Client") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await mqtt_publish_tool(
            exec_context=exec_context,
            topic="home/switch/command",
            payload="ON",
        )

    data = result.get_data()
    assert isinstance(data, dict)
    assert data["payload_size"] == 2  # "ON" without JSON quotes

    call_args = mock_client.publish.call_args
    assert call_args.kwargs["payload"] == b"ON"


@pytest.mark.asyncio
async def test_mqtt_publish_list_payload(exec_context: ToolExecutionContext) -> None:
    mock_client = AsyncMock()
    with patch("family_assistant.tools.mqtt.aiomqtt.Client") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        await mqtt_publish_tool(
            exec_context=exec_context,
            topic="home/items",
            payload=["milk", "eggs"],
        )

    call_args = mock_client.publish.call_args
    assert call_args.kwargs["payload"] == b'["milk", "eggs"]'


@pytest.mark.asyncio
async def test_mqtt_publish_not_configured(
    exec_context_no_mqtt: ToolExecutionContext,
) -> None:
    with pytest.raises(ValueError, match="MQTT broker not configured"):
        await mqtt_publish_tool(
            exec_context=exec_context_no_mqtt,
            topic="test/topic",
            payload={"key": "value"},
        )


@pytest.mark.asyncio
async def test_mqtt_publish_no_processing_service() -> None:
    context = ToolExecutionContext(
        conversation_id="test-conv",
        user_name="test-user",
        interface_type="test",
        timezone=ZoneInfo("UTC"),
        turn_id=None,
        db_context=MagicMock(spec=Database),
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        credential_resolvers=None,
        api_backend=None,
    )
    with pytest.raises(ValueError, match="MQTT broker not configured"):
        await mqtt_publish_tool(
            exec_context=context,
            topic="test/topic",
            payload={"key": "value"},
        )


@pytest.mark.asyncio
async def test_mqtt_publish_broker_connection_error(
    exec_context: ToolExecutionContext,
) -> None:
    with patch("family_assistant.tools.mqtt.aiomqtt.Client") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.side_effect = aiomqtt.MqttError(
            "Connection refused"
        )

        with pytest.raises(ValueError, match="MQTT error publishing to"):
            await mqtt_publish_tool(
                exec_context=exec_context,
                topic="test/topic",
                payload={"key": "value"},
            )


@pytest.mark.asyncio
async def test_mqtt_publish_timeout(exec_context: ToolExecutionContext) -> None:
    with patch("family_assistant.tools.mqtt.aiomqtt.Client") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.side_effect = TimeoutError

        with pytest.raises(ValueError, match="Timed out connecting to MQTT broker"):
            await mqtt_publish_tool(
                exec_context=exec_context,
                topic="test/topic",
                payload={"key": "value"},
            )
