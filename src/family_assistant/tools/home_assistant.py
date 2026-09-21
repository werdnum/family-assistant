"""Home Assistant integration tools.

This module contains tools for interacting with Home Assistant, including
rendering templates and retrieving camera snapshots.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant_api.errors import HomeassistantAPIError

from family_assistant.tools.types import (
    ToolAttachment,
    ToolDefinition,
    ToolResult,
    get_attachment_limits,
)

if TYPE_CHECKING:
    from family_assistant.home_assistant_wrapper import ActionPayload
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


def detect_image_mime_type(content: bytes) -> str:
    """
    Detect MIME type from image content based on file signatures.

    Args:
        content: The binary image content

    Returns:
        The detected MIME type string, defaults to "image/jpeg"
    """
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    elif content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    elif content.startswith(b"GIF87a") or content.startswith(b"GIF89a"):
        return "image/gif"
    elif content.startswith(b"RIFF") and b"WEBP" in content[:12]:
        return "image/webp"
    else:
        return "image/jpeg"  # Default fallback


# Tool Definitions
HOME_ASSISTANT_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "download_state_history",
            "description": (
                "Downloads historical state data from Home Assistant as a JSON file. "
                "This tool retrieves past state changes for specified entities over a given time period, "
                "allowing analysis and manipulation of historical data.\n\n"
                "Returns: A JSON attachment containing the state history data with entity states, attributes, "
                "and timestamps. The data can be loaded and analyzed programmatically. "
                "If no entities are specified, retrieves history for all entities (may be large). "
                "On errors, returns descriptive error messages.\n\n"
                "Response Schema:\n"
                "{\n"
                '  "start_time": "ISO 8601 timestamp",\n'
                '  "end_time": "ISO 8601 timestamp",\n'
                '  "significant_changes_only": boolean,\n'
                '  "entities": [\n'
                "    {\n"
                '      "entity_id": "sensor.example",\n'
                '      "states": [\n'
                "        {\n"
                '          "state": "value or unavailable/unknown/null",\n'
                '          "attributes": {...},\n'
                '          "last_changed": "ISO 8601 timestamp",\n'
                '          "last_updated": "ISO 8601 timestamp"\n'
                "        }\n"
                "      ]\n"
                "    }\n"
                "  ]\n"
                "}\n\n"
                "IMPORTANT: For data visualization, it's recommended to retrieve history as an attachment first "
                "(using this tool), then pass the attachment to visualization tools. This allows the LLM to see "
                "the inferred JSON schema, making it much easier to understand the data structure and create "
                "correct visualizations. Sensor states may contain non-numeric values like 'unavailable' or 'unknown' "
                "that should be filtered before visualization."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional list of entity IDs to retrieve history for (e.g., ['sensor.temperature', 'light.living_room']). "
                            "If not provided, retrieves history for all entities. Be cautious with all entities as it may return a large dataset."
                        ),
                    },
                    "start_time": {
                        "type": "string",
                        "description": (
                            "Optional ISO 8601 timestamp for the start of the history period (e.g., '2024-01-01T00:00:00Z'). "
                            "If not provided, defaults to 24 hours ago."
                        ),
                    },
                    "end_time": {
                        "type": "string",
                        "description": (
                            "Optional ISO 8601 timestamp for the end of the history period (e.g., '2024-01-02T00:00:00Z'). "
                            "If not provided, defaults to current time."
                        ),
                    },
                    "significant_changes_only": {
                        "type": "boolean",
                        "description": (
                            "If true, only return significant state changes (filters out minor updates). "
                            "Defaults to false."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "render_home_assistant_template",
            "description": (
                "Renders a Home Assistant Jinja2 template and returns the result. "
                "This tool allows you to evaluate templates using Home Assistant's current state, "
                "including all entities, attributes, and template functions available in HA. "
                "Common uses include getting entity states, performing calculations, "
                "or formatting data using Home Assistant's template engine.\n\n"
                "Returns: A string containing the rendered template result. "
                "On success, returns the evaluated template output as a string (empty results return 'Template rendered to empty result'). "
                "If HA not configured, returns 'Error: Home Assistant integration is not configured or available.'. "
                "If HA API not installed, returns 'Error: Home Assistant API library is not installed.'. "
                "On API error, returns 'Error: Home Assistant API error - [error details]'. "
                "On other errors, returns 'Error: Failed to render template - [error details]'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "template": {
                        "type": "string",
                        "description": (
                            "The Jinja2 template string to render. Can use all Home Assistant "
                            "template functions and filters, such as states(), state_attr(), "
                            "now(), as_timestamp(), etc. Example: '{{ states(\"sensor.temperature\") }}'"
                        ),
                    },
                },
                "required": ["template"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_camera_snapshot",
            "description": (
                "Retrieves a current snapshot/image from a Home Assistant camera entity. "
                "The image will be displayed to you for analysis. "
                "If no camera_entity_id is provided, returns a list of available cameras.\n\n"
                "Common camera entities include doorbell cameras, security cameras, and webcams. "
                "Examples: camera.front_door, camera.doorbell_camera, camera.backyard_cam\n\n"
                "Returns: Captures and displays the camera image to the user when entity_id is provided. "
                "Without entity_id, returns list of available cameras. "
                "On errors, returns descriptive error messages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "camera_entity_id": {
                        "type": "string",
                        "description": (
                            "The Home Assistant entity ID of the camera (e.g., 'camera.front_door'). "
                            "If not provided, returns a list of all available camera entities."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "call_home_assistant_action",
            "description": (
                "Execute an arbitrary Home Assistant action (formerly known as a "
                "'service call'). Use this to control devices and trigger automations: "
                "turn lights on/off, lock/unlock doors, set climate temperatures, "
                "play media, send notifications via HA, etc.\n\n"
                "An action is identified by a `domain` and an `action` name. The action "
                "name corresponds to what Home Assistant historically called the "
                "'service' (e.g. domain='light', action='turn_on'). Use "
                "`list_home_assistant_entities` first to discover entity IDs if you are "
                "not sure of them.\n\n"
                "Common examples:\n"
                "- Turn on a light: domain='light', action='turn_on', "
                "service_data={'entity_id': 'light.kitchen', 'brightness_pct': 75}\n"
                "- Turn off a switch: domain='switch', action='turn_off', "
                "service_data={'entity_id': 'switch.fan'}\n"
                "- Set thermostat: domain='climate', action='set_temperature', "
                "service_data={'entity_id': 'climate.living_room', 'temperature': 21}\n"
                "- Activate a scene: domain='scene', action='turn_on', "
                "service_data={'entity_id': 'scene.movie_night'}\n"
                "- Trigger a script: domain='script', action='turn_on', "
                "service_data={'entity_id': 'script.bedtime'}\n\n"
                "Returns: A summary of state changes that occurred during the action, "
                "including each affected entity and its new state. If "
                "`return_response=true`, also includes the action's response payload "
                "(only supported by actions that declare `supports_response`). On "
                "errors, returns a descriptive error message."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": (
                            "The Home Assistant action domain (also the entity domain "
                            "for most actions), e.g. 'light', 'switch', 'climate', "
                            "'scene', 'script', 'media_player', 'notify'."
                        ),
                    },
                    "action": {
                        "type": "string",
                        "description": (
                            "The action name within the domain (this is what HA "
                            "previously called the 'service'), e.g. 'turn_on', "
                            "'turn_off', 'toggle', 'set_temperature', 'lock', 'unlock'."
                        ),
                    },
                    "service_data": {
                        "type": "object",
                        "description": (
                            "Optional payload for the action. Typically includes "
                            "`entity_id` (a string or list of strings) identifying the "
                            "target entity/entities, plus any action-specific fields "
                            "(e.g. `brightness_pct`, `temperature`, `message`). May "
                            "also use the HA `target` block (with `entity_id`, "
                            "`device_id`, or `area_id`)."
                        ),
                        "additionalProperties": True,
                    },
                    "return_response": {
                        "type": "boolean",
                        "description": (
                            "If true, request the action's response payload. Only "
                            "supported for actions declared with `supports_response` in "
                            "Home Assistant (e.g. some `calendar.*` and `weather.*` "
                            "actions). Defaults to false."
                        ),
                        "default": False,
                    },
                },
                "required": ["domain", "action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_home_assistant_entities",
            "description": (
                "List and search Home Assistant entities by ID, name, or area. "
                "Returns entities with their IDs, friendly names, areas, and devices. "
                "Useful for discovering what sensors, lights, switches, cameras, and other entities "
                "are available in your Home Assistant setup.\n\n"
                "The entity_id_filter parameter does substring matching, so you can search by:\n"
                "- Entity type: 'sensor', 'light', 'switch', 'binary_sensor'\n"
                "- Function: 'temperature', 'motion', 'energy', 'camera'\n"
                "- Specific entity: 'pool', 'living_room', 'garage'\n"
                "- Combined: 'sensor.pool' finds pool sensors, 'light.living' finds living room lights\n\n"
                "Examples:\n"
                "- list_home_assistant_entities(entity_id_filter='temperature') → all temperature sensors\n"
                "- list_home_assistant_entities(entity_id_filter='light.living') → living room lights\n"
                "- list_home_assistant_entities(area_filter='pool') → all pool equipment\n"
                "- list_home_assistant_entities(entity_id_filter='motion') → motion sensors\n\n"
                "Results are cached for 2 minutes to improve performance."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id_filter": {
                        "type": "string",
                        "description": (
                            "Optional case-insensitive substring to filter entity IDs. "
                            "Since entity IDs follow the pattern 'domain.name' (e.g., 'sensor.pool_temperature'), "
                            "you can filter by domain ('sensor'), function ('temperature'), location ('pool'), "
                            "or combinations ('sensor.pool'). Matches any part of the entity ID."
                        ),
                    },
                    "area_filter": {
                        "type": "string",
                        "description": (
                            "Optional case-insensitive substring to filter by area name "
                            "(e.g., 'living room', 'pool', 'garage', 'bedroom'). "
                            "Only returns entities assigned to areas matching this substring."
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "description": (
                            "Maximum number of results to return. Defaults to 50. "
                            "Maximum allowed is 200. Use filters to narrow results if needed."
                        ),
                        "default": 50,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_home_assistant_actions",
            "description": (
                "Discover the actions (formerly 'services') currently exposed by "
                "the connected Home Assistant instance. The catalog is fetched "
                "live from HA's `GET /api/services` endpoint, so it always "
                "matches the integrations actually installed — there is no "
                "static list to keep in sync with the HA version.\n\n"
                "Use this BEFORE calling `call_home_assistant_action` so you "
                "know which `domain` / `action` names exist and which fields "
                "they accept. Workflow:\n"
                "  1. Call without arguments to see all available domains and actions.\n"
                "  2. Call with `domain='light'` (or whatever domain you need) "
                "to see the field schema for each action in that domain.\n"
                "  3. Optionally narrow further with `action_filter` to find "
                "actions whose name contains a substring (e.g. 'turn_on').\n\n"
                "Each entry returns the `domain`, `action` (service id), human "
                "`name`/`description`, the `fields` schema (per-parameter HA "
                "selectors describing accepted values), the optional `target` "
                "selector block, and `supports_response` (whether the action "
                "supports `return_response=true`)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": (
                            "Optional HA domain to narrow results, e.g. "
                            "'light', 'switch', 'climate', 'scene', 'script'. "
                            "Omit to list every domain."
                        ),
                    },
                    "action_filter": {
                        "type": "string",
                        "description": (
                            "Optional case-insensitive substring matched against "
                            "the action name (e.g. 'turn_on', 'set_'). Combine "
                            "with `domain` to narrow further."
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "description": (
                            "Maximum number of catalog entries to return. "
                            "Defaults to 100. Use filters to narrow results."
                        ),
                        "default": 100,
                    },
                },
                "required": [],
            },
        },
    },
]


# Tool Implementation
async def render_home_assistant_template_tool(
    exec_context: ToolExecutionContext,
    template: str,
) -> str:
    """
    Renders a Home Assistant template and returns the result.

    Args:
        exec_context: The tool execution context
        template: The Jinja2 template string to render

    Returns:
        The rendered template result as a string, or an error message
    """
    logger.info(f"Rendering Home Assistant template: {template[:100]}...")

    # Check if Home Assistant client is available in context
    if (
        not hasattr(exec_context, "home_assistant_client")
        or not exec_context.home_assistant_client
    ):
        logger.error("Home Assistant client not available in execution context")
        return "Error: Home Assistant integration is not configured or available."

    ha_client = exec_context.home_assistant_client

    try:
        # Import homeassistant_api to check for the method
        from homeassistant_api.errors import (  # noqa: PLC0415
            HomeassistantAPIError,
        )
    except ImportError:
        logger.error("homeassistant_api library is not installed")
        return "Error: Home Assistant API library is not installed."

    try:
        # Use the async method to render the template
        rendered_result = await ha_client.async_get_rendered_template(template=template)

        if rendered_result is None:
            logger.warning("Template rendering returned None")
            return "Template rendered to empty result"

        # Convert to string if needed
        result_str = str(rendered_result).strip()

        logger.info(f"Successfully rendered template, result length: {len(result_str)}")
        return result_str

    except HomeassistantAPIError as e:
        logger.exception(f"Home Assistant API error rendering template: {e}")
        return f"Error: Home Assistant API error - {e!s}"
    except Exception as e:
        logger.exception(f"Unexpected error rendering template: {e}")
        return f"Error: Failed to render template - {e!s}"


async def get_camera_snapshot_tool(
    exec_context: ToolExecutionContext,
    camera_entity_id: str | None = None,
) -> ToolResult:
    """
    Retrieves a snapshot from a Home Assistant camera or lists available cameras.

    Args:
        exec_context: The tool execution context containing HA client
        camera_entity_id: The entity ID of the camera (e.g., 'camera.front_door')
                         If not provided, returns list of available cameras.

    Returns:
        ToolResult with image attachment when entity_id is provided,
        or string with list of available cameras when entity_id is omitted
    """
    logger.info(f"Getting camera snapshot: entity_id={camera_entity_id}")

    # Check if Home Assistant client is available
    if (
        not hasattr(exec_context, "home_assistant_client")
        or not exec_context.home_assistant_client
    ):
        logger.error("Home Assistant client not available in execution context")
        return ToolResult(
            text="Error: Home Assistant integration is not configured or available."
        )

    ha_client = exec_context.home_assistant_client

    # If no entity_id provided, list available cameras
    if not camera_entity_id:
        try:
            # Get all entities to find cameras
            states = await ha_client.async_get_states()
        except Exception as e:
            logger.exception(f"Error listing cameras: {e}")
            return ToolResult(text=f"Error listing available cameras: {e!s}")

        # Filter for camera entities
        cameras = []
        for entity in states:
            if entity.entity_id.startswith("camera."):
                # Get friendly name if available
                friendly_name = entity.attributes.get("friendly_name", entity.entity_id)
                if friendly_name and friendly_name != entity.entity_id:
                    cameras.append(f"- {entity.entity_id} ({friendly_name})")
                else:
                    cameras.append(f"- {entity.entity_id}")

        if not cameras:
            return ToolResult(text="No camera entities found in Home Assistant.")

        return ToolResult(
            text="Available cameras in Home Assistant:\n" + "\n".join(cameras)
        )

    # Use the HA client's custom camera snapshot method to get raw binary data
    try:
        image_content = await ha_client.async_get_camera_snapshot(camera_entity_id)
    except Exception as e:
        logger.exception(f"Error getting camera snapshot: {e}")
        return ToolResult(text=f"Error: Failed to retrieve camera snapshot: {e!s}")

    # Check image size (multimodal limit from config)
    image_size = len(image_content)
    _, max_multimodal_size = get_attachment_limits(exec_context)
    if image_size > max_multimodal_size:
        max_mb = max_multimodal_size / (1024 * 1024)
        logger.warning(
            f"Camera image is {image_size / (1024 * 1024):.1f}MB, exceeds {max_mb:.0f}MB limit"
        )
        return ToolResult(
            text=f"Error: Camera image too large ({image_size / (1024 * 1024):.1f}MB), exceeds {max_mb:.0f}MB limit"
        )

    logger.info(f"Successfully retrieved camera snapshot: {image_size} bytes")

    # Detect MIME type from image content
    mime_type = detect_image_mime_type(image_content)

    # Return image as attachment
    return ToolResult(
        text=f"Retrieved snapshot from camera '{camera_entity_id}'",
        attachments=[
            ToolAttachment(
                mime_type=mime_type,
                content=image_content,
                description=f"Camera snapshot from {camera_entity_id}",
            )
        ],
    )


async def download_state_history_tool(
    exec_context: ToolExecutionContext,
    entity_ids: list[str] | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    significant_changes_only: bool = False,
) -> ToolResult:
    """
    Downloads Home Assistant state history as a JSON attachment.

    Args:
        exec_context: The tool execution context containing HA client
        entity_ids: Optional list of entity IDs to retrieve history for
        start_time: Optional ISO 8601 timestamp for start of period
        end_time: Optional ISO 8601 timestamp for end of period
        significant_changes_only: If true, only significant state changes

    Returns:
        ToolResult with JSON attachment containing state history data
    """
    logger.info(
        f"Downloading state history: entities={entity_ids}, start={start_time}, "
        f"end={end_time}, significant_only={significant_changes_only}"
    )

    # Check if Home Assistant client is available
    if (
        not hasattr(exec_context, "home_assistant_client")
        or not exec_context.home_assistant_client
    ):
        logger.error("Home Assistant client not available in execution context")
        return ToolResult(
            text="Error: Home Assistant integration is not configured or available."
        )

    ha_client = exec_context.home_assistant_client

    # Parse end_time first to determine default start_time
    if end_time:
        try:
            end_timestamp = datetime.fromisoformat(
                end_time.replace("Z", "+00:00")
            ).astimezone(UTC)
        except (ValueError, AttributeError) as e:
            logger.exception(f"Error parsing timestamps: {e}")
            return ToolResult(
                text=f"Error: Invalid timestamp format. Use ISO 8601 format (e.g., '2024-01-01T00:00:00Z'): {e!s}"
            )
    else:
        # Default to now
        end_timestamp = datetime.now(UTC)

    if start_time:
        try:
            start_timestamp = datetime.fromisoformat(
                start_time.replace("Z", "+00:00")
            ).astimezone(UTC)
        except (ValueError, AttributeError) as e:
            logger.exception(f"Error parsing timestamps: {e}")
            return ToolResult(
                text=f"Error: Invalid timestamp format. Use ISO 8601 format (e.g., '2024-01-01T00:00:00Z'): {e!s}"
            )
    else:
        # Default to 24 hours before end_time
        start_timestamp = end_timestamp - timedelta(days=1)

    # Validate that start_time is before end_time
    if start_timestamp >= end_timestamp:
        return ToolResult(
            text=f"Error: start_time ({start_timestamp.isoformat()}) must be before end_time ({end_timestamp.isoformat()})"
        )

    # Retrieve history
    histories = []

    # If specific entity IDs are requested, fetch State objects in bulk
    states = None
    if entity_ids:
        # Fetch all states in one bulk call instead of looping
        try:
            all_states = await ha_client.async_get_states()
        except Exception as e:
            logger.exception(f"Error retrieving state history: {e}")
            return ToolResult(text=f"Error: Failed to retrieve state history: {e!s}")

        # Filter to requested entities
        entity_id_set = set(entity_ids)
        states = []
        for state in all_states:
            if state.entity_id in entity_id_set:
                states.append(state)

        # Error out on any missing entities
        found_ids = {s.entity_id for s in states}
        missing_ids = entity_id_set - found_ids
        if missing_ids:
            logger.error(
                f"Requested entities not found in Home Assistant: {missing_ids}"
            )
            return ToolResult(
                text=f"Error: The following entities were not found in Home Assistant: {', '.join(sorted(missing_ids))}"
            )

        states = tuple(states) if states else None

    # Use library's async_get_entity_histories with State objects
    try:
        async for history in ha_client._client.async_get_entity_histories(
            entities=states,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            significant_changes_only=significant_changes_only,
        ):
            # Use Pydantic's model_dump for proper JSON serialization
            history_dict = history.model_dump(mode="json")
            # Add the entity_id property (computed, not in model_dump)
            history_dict["entity_id"] = history.entity_id
            histories.append(history_dict)
    except Exception as e:
        logger.exception(f"Error retrieving state history: {e}")
        return ToolResult(text=f"Error: Failed to retrieve state history: {e!s}")

    if not histories:
        return ToolResult(text="No history data found for the specified parameters.")

    # Convert timestamps to user's local timezone
    local_tz = exec_context.timezone
    for history_dict in histories:
        for state in history_dict.get("states", []):
            for ts_field in ("last_changed", "last_updated"):
                ts_val = state.get(ts_field)
                if isinstance(ts_val, str) and ts_val:
                    parsed = datetime.fromisoformat(ts_val)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                    state[ts_field] = parsed.astimezone(local_tz).isoformat()

    # Convert to JSON
    json_data = json.dumps(
        {
            "start_time": start_timestamp.astimezone(local_tz).isoformat(),
            "end_time": end_timestamp.astimezone(local_tz).isoformat(),
            "significant_changes_only": significant_changes_only,
            "entities": histories,
        },
        indent=2,
    )

    json_bytes = json_data.encode("utf-8")

    # Check size limits
    max_text_size, _ = get_attachment_limits(exec_context)
    if len(json_bytes) > max_text_size:
        max_mb = max_text_size / (1024 * 1024)
        logger.warning(
            f"History data is {len(json_bytes) / (1024 * 1024):.1f}MB, exceeds {max_mb:.0f}MB limit"
        )
        return ToolResult(
            text=f"Error: History data too large ({len(json_bytes) / (1024 * 1024):.1f}MB), "
            f"exceeds {max_mb:.0f}MB limit. Try reducing the time range or number of entities."
        )

    logger.info(
        f"Successfully retrieved state history: {len(histories)} entities, {len(json_bytes)} bytes"
    )

    # Build description
    entity_count = len(histories)
    state_count = sum(len(h["states"]) for h in histories)
    description = (
        f"State history for {entity_count} entities ({state_count} states) "
        f"from {start_timestamp.astimezone(local_tz).strftime('%Y-%m-%d %H:%M:%S %Z')} to {end_timestamp.astimezone(local_tz).strftime('%Y-%m-%d %H:%M:%S %Z')}"
    )

    # Return JSON as attachment
    return ToolResult(
        text=description,
        attachments=[
            ToolAttachment(
                mime_type="application/json",
                content=json_bytes,
                description=description,
            )
        ],
    )


async def call_home_assistant_action_tool(
    exec_context: ToolExecutionContext,
    domain: str,
    action: str,
    service_data: ActionPayload | None = None,
    return_response: bool = False,
) -> ToolResult:
    """
    Execute a Home Assistant action (formerly known as a "service call").

    Args:
        exec_context: The tool execution context containing the HA client.
        domain: The action domain (e.g., 'light', 'switch', 'climate').
        action: The action name within the domain (e.g., 'turn_on').
        service_data: Optional dict with the action payload (entity_id, target,
            and any action-specific fields).
        return_response: If true, request the action's response payload.

    Returns:
        ToolResult with structured data describing the changed states and any
        action response payload, or an error message.
    """
    logger.info(
        "Calling Home Assistant action: %s.%s data=%s return_response=%s",
        domain,
        action,
        service_data,
        return_response,
    )

    if (
        not hasattr(exec_context, "home_assistant_client")
        or not exec_context.home_assistant_client
    ):
        logger.error("Home Assistant client not available in execution context")
        return ToolResult(
            text="Error: Home Assistant integration is not configured or available."
        )

    ha_client = exec_context.home_assistant_client

    try:
        result = await ha_client.async_call_action(
            domain=domain,
            action=action,
            service_data=service_data,
            return_response=return_response,
        )
    except HomeassistantAPIError as e:
        logger.exception(
            "Home Assistant API error calling %s.%s: %s", domain, action, e
        )
        return ToolResult(
            text=f"Error: Home Assistant API error calling {domain}.{action} - {e!s}"
        )

    changed_states = result.get("changed_states", [])
    response_payload = result.get("response", {})

    summary_parts = [f"Called {domain}.{action}"]
    if changed_states:
        summary_parts.append(
            f"{len(changed_states)} state change(s): "
            + ", ".join(
                f"{state.get('entity_id', '?')}={state.get('state', '?')}"
                for state in changed_states
            )
        )
    else:
        summary_parts.append("no state changes reported")

    if return_response:
        summary_parts.append(f"response={json.dumps(response_payload)}")

    summary = "; ".join(summary_parts)

    # ast-grep-ignore: no-dict-any - tool result data structure
    data: dict[str, Any] = {
        "domain": domain,
        "action": action,
        "changed_states": changed_states,
    }
    if return_response:
        data["response"] = response_payload

    return ToolResult(text=summary, data=data)


async def list_home_assistant_actions_tool(
    exec_context: ToolExecutionContext,
    domain: str | None = None,
    action_filter: str | None = None,
    max_results: int = 100,
) -> ToolResult:
    """List the actions currently exposed by the connected HA instance.

    The catalog is fetched live from HA's ``/api/services`` endpoint so it
    always matches the integrations actually installed on the user's HA —
    there is no static mapping to drift out of sync with the HA version.

    Args:
        exec_context: The tool execution context containing the HA client.
        domain: Optional HA domain to narrow results (e.g. ``"light"``).
        action_filter: Optional case-insensitive substring matched against
            the action name.
        max_results: Cap on the number of catalog entries returned (default
            100, hard cap 500).

    Returns:
        ToolResult with structured data: ``{"actions": [...], "total_matches": N}``.
        Each entry includes ``domain``, ``action``, ``name``, ``description``,
        ``fields``, ``target``, and ``supports_response``.
    """
    logger.info(
        "Listing HA actions: domain=%s action_filter=%s max=%d",
        domain,
        action_filter,
        max_results,
    )

    if (
        not hasattr(exec_context, "home_assistant_client")
        or not exec_context.home_assistant_client
    ):
        logger.error("Home Assistant client not available in execution context")
        return ToolResult(
            text="Error: Home Assistant integration is not configured or available."
        )

    ha_client = exec_context.home_assistant_client
    max_results = max(1, min(max_results, 500))

    try:
        catalog = await ha_client.async_get_action_catalog(domain=domain)
    except HomeassistantAPIError as e:
        logger.error("Home Assistant API error fetching action catalog: %s", e)
        return ToolResult(
            text=f"Error: Home Assistant API error fetching action catalog - {e!s}"
        )

    filtered = catalog
    if action_filter:
        needle = action_filter.lower()
        filtered = [entry for entry in filtered if needle in entry["action"].lower()]

    total_matches = len(filtered)
    result_entries = filtered[:max_results]

    # ast-grep-ignore: no-dict-any - tool result data structure mirrors HA's action catalog
    result_data: dict[str, Any] = {
        "actions": result_entries,
        "total_matches": total_matches,
    }
    # ast-grep-ignore: no-dict-any - filter info dict mirrors arguments
    filters_applied: dict[str, Any] = {}
    if domain:
        filters_applied["domain"] = domain
    if action_filter:
        filters_applied["action_filter"] = action_filter
    if filters_applied:
        result_data["filters_applied"] = filters_applied

    if total_matches == 0:
        text = (
            "No matching Home Assistant actions found"
            + (f" for domain={domain!r}" if domain else "")
            + (f" matching {action_filter!r}" if action_filter else "")
            + "."
        )
    else:
        header = (
            f"Found {total_matches} Home Assistant action(s)"
            if total_matches <= max_results
            else f"Found {total_matches} Home Assistant action(s); showing first {max_results}"
        )
        lines = [header + ":"]
        for entry in result_entries:
            line = f"- {entry['domain']}.{entry['action']}"
            description = entry.get("description")
            if description:
                line += f" — {description}"
            if entry.get("supports_response"):
                line += " [supports response]"
            lines.append(line)
        text = "\n".join(lines)

    return ToolResult(text=text, data=result_data)


async def list_home_assistant_entities_tool(
    exec_context: ToolExecutionContext,
    entity_id_filter: str | None = None,
    area_filter: str | None = None,
    max_results: int = 50,
) -> ToolResult:
    """
    List and search Home Assistant entities with filtering.

    Args:
        exec_context: The tool execution context containing HA client
        entity_id_filter: Optional case-insensitive substring to filter entity IDs
        area_filter: Optional case-insensitive substring to filter by area name
        max_results: Maximum number of results to return (default: 50, max: 200)

    Returns:
        ToolResult with structured data containing matching entities
    """
    logger.info(
        f"Listing HA entities: entity_filter={entity_id_filter}, "
        f"area_filter={area_filter}, max={max_results}"
    )

    # Check if Home Assistant client is available
    if (
        not hasattr(exec_context, "home_assistant_client")
        or not exec_context.home_assistant_client
    ):
        logger.error("Home Assistant client not available in execution context")
        return ToolResult(
            text="Error: Home Assistant integration is not configured or available."
        )

    ha_client = exec_context.home_assistant_client

    # Limit max_results to 200
    max_results = min(max_results, 200)

    try:
        # Get entities from client (with built-in caching)
        entities = await ha_client.async_get_entity_list_with_metadata()
    except Exception as e:
        logger.exception(f"Error listing entities: {e}")
        return ToolResult(text=f"Error: Failed to list entities: {e!s}")

    # Apply filters
    filtered = entities

    if entity_id_filter:
        filter_lower = entity_id_filter.lower()
        filtered = [e for e in filtered if filter_lower in e["entity_id"].lower()]
        logger.debug(f"After entity_id filter: {len(filtered)} entities")

    if area_filter:
        filter_lower = area_filter.lower()
        filtered = [
            e
            for e in filtered
            if (area_name := e.get("area_name")) and filter_lower in area_name.lower()
        ]
        logger.debug(f"After area filter: {len(filtered)} entities")

    # Limit results
    total_matches = len(filtered)
    result_entities = filtered[:max_results]

    logger.info(
        f"Returning {len(result_entities)} of {total_matches} matching entities"
    )

    # Build result data
    # ast-grep-ignore: no-dict-any - Tool result data structure
    result_data: dict[str, Any] = {
        "entities": result_entities,
        "total_matches": total_matches,
    }

    # Add filter info if filters were applied
    if entity_id_filter or area_filter:
        # ast-grep-ignore: no-dict-any - Filter info structure
        filters_applied: dict[str, Any] = {}
        if entity_id_filter:
            filters_applied["entity_id_filter"] = entity_id_filter
        if area_filter:
            filters_applied["area_filter"] = area_filter
        result_data["filters_applied"] = filters_applied

    # Build text summary with actual entity details
    if total_matches == 0:
        text = "No matching entities found."
    else:
        # Build header
        if total_matches <= max_results:
            text = f"Found {total_matches} matching entities:\n\n"
        else:
            text = f"Found {total_matches} matching entities. Showing first {max_results}:\n\n"

        # List each entity with details
        for entity in result_entities:
            entity_id = entity.get("entity_id", "unknown")
            name = entity.get("name", entity_id)
            area = entity.get("area_name")
            device = entity.get("device_name")

            # Build entity line with available metadata
            text += f"- {entity_id}"
            if name and name != entity_id:
                text += f" - {name}"
            if area:
                text += f" (Area: {area})"
            if device:
                text += f" [Device: {device}]"
            text += "\n"

        # Add filter info if filters were applied
        if entity_id_filter or area_filter:
            filter_desc = []
            if entity_id_filter:
                filter_desc.append(f"entity_id contains '{entity_id_filter}'")
            if area_filter:
                filter_desc.append(f"area contains '{area_filter}'")
            text += f"\nFilters applied: {', '.join(filter_desc)}"

    return ToolResult(text=text, data=result_data)
