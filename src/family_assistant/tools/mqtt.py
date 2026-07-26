"""MQTT publish tool for sending messages to MQTT brokers.

Provides a generic mqtt_publish tool that can be used to push structured data
to any MQTT topic. Useful for updating e-ink displays, dashboards, ESPHome
devices, or any MQTT-connected system.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

import aiomqtt

from family_assistant.tools.types import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

MQTT_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "mqtt_publish",
            "description": (
                "Publish a message to an MQTT topic. Use this to push data to external "
                "devices and services (e-ink displays, ESPHome devices, dashboards, "
                "Home Assistant, etc.). The payload can be any JSON value: an object, "
                "array, string, number, or boolean. Messages are retained by default "
                "so devices get the latest value on reconnect."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": (
                            "The MQTT topic to publish to (e.g. 'family/eink/display', "
                            "'home/status/summary')."
                        ),
                    },
                    "payload": {
                        "description": "The payload to publish. Any JSON value (object, array, string, number, boolean).",
                    },
                    "retain": {
                        "type": "boolean",
                        "description": (
                            "Whether the broker should retain this message for new subscribers. "
                            "Default true."
                        ),
                        "default": True,
                    },
                },
                "required": ["topic", "payload"],
            },
        },
    },
]


def _get_mqtt_config(
    exec_context: ToolExecutionContext,
) -> tuple[str, int, str | None, str | None]:
    """Extract MQTT config from the execution context.

    Returns:
        Tuple of (host, port, username, password).

    Raises:
        ValueError: If MQTT broker host is not configured.
    """
    if exec_context.processing_service and exec_context.processing_service.app_config:
        mqtt_config = exec_context.processing_service.app_config.mqtt_config
        if mqtt_config.broker_host:
            return (
                mqtt_config.broker_host,
                mqtt_config.broker_port,
                mqtt_config.username,
                mqtt_config.password,
            )

    msg = (
        "MQTT broker not configured. Set MQTT_BROKER_HOST environment variable "
        "or configure mqtt_config.broker_host in config.yaml."
    )
    raise ValueError(msg)


async def mqtt_publish_tool(
    exec_context: ToolExecutionContext,
    topic: str,
    payload: dict[str, object] | list[object] | str | float | bool,
    retain: bool = True,
) -> ToolResult:
    """Publish a JSON message to an MQTT topic."""
    host, port, username, password = _get_mqtt_config(exec_context)

    payload_bytes = (
        payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
    )

    logger.info(
        "Publishing to MQTT topic %s (retain=%s, %d bytes)",
        topic,
        retain,
        len(payload_bytes),
    )

    try:
        async with asyncio.timeout(10):
            async with aiomqtt.Client(
                hostname=host,
                port=port,
                username=username,
                password=password,
            ) as client:
                await client.publish(topic, payload=payload_bytes, retain=retain)
    except TimeoutError:
        msg = f"Timed out connecting to MQTT broker {host}:{port}"
        raise ValueError(msg) from None
    except aiomqtt.MqttError as e:
        msg = f"MQTT error publishing to {topic}: {e}"
        raise ValueError(msg) from e

    return ToolResult(
        data={
            "topic": topic,
            "retain": retain,
            "payload_size": len(payload_bytes),
        }
    )
