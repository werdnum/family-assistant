"""Home Assistant tools: templates, entities, actions, history and camera snapshots."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant_api.errors import HomeassistantAPIError

from family_assistant.plugins.home_assistant.client import (
    CameraSnapshotHTTPError,
    CameraSnapshotTimeoutError,
    EntityMetadata,
)
from family_assistant.plugins.home_assistant.instance import HomeAssistantInstance
from family_assistant.tools.metadata import (
    ToolRegistration,
    ToolTag,
    make_local_tool_metadata,
)
from family_assistant.tools.types import (
    ToolAttachment,
    ToolDefinition,
    ToolResult,
    get_attachment_limits,
)

if TYPE_CHECKING:
    from family_assistant.plugins.home_assistant.client import (
        ActionCatalogEntry,
        ActionPayload,
        HomeAssistantClientWrapper,
    )
    from family_assistant.tools.metadata import ToolImplementation
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

GET_HOME_STATUS_TOOL_NAME = "get_home_status"

# A narrowed lookup shows each action's fields, which is what calling it
# needs; a broad listing stays to names, since every field of every action in
# a domain like "media_player" runs to tens of kilobytes.
ACTION_FIELD_DETAIL_LIMIT = 10
ACTION_DETAIL_VALUE_CHARS = 160

DEFAULT_ENTITY_RESULTS = 25
MAX_ENTITY_RESULTS = 200
# A query word matches an entity word it is a prefix of, or that is a prefix of
# it ("temp" / "temperature"), but only from this length: shorter prefixes
# ("t", "on") match nearly everything.
MIN_PREFIX_MATCH_LENGTH = 3


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
                "Find Home Assistant entities and read their current state. "
                "Each result gives the entity ID, friendly name, area, device "
                "and its state right now (with unit), so a question like "
                "'what's the pool temperature?' is usually answered by one "
                "call.\n\n"
                "Give at least one of `query`, `entity_id_filter` or "
                "`area_filter`; the full list is too long to be useful.\n\n"
                "Examples:\n"
                "- list_home_assistant_entities(query='pool temperature')\n"
                "- list_home_assistant_entities(query='front door lock')\n"
                "- list_home_assistant_entities(area_filter='kitchen')\n"
                "- list_home_assistant_entities(entity_id_filter='light.')"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Words describing what you are looking for, e.g. "
                            "'pool temperature' or 'garage door'. Matched "
                            "against entity ID, name, area, device and sensor "
                            "type, best matches first. A word also matches "
                            "its abbreviation, so 'temperature' finds 'temp'."
                        ),
                    },
                    "entity_id_filter": {
                        "type": "string",
                        "description": (
                            "Case-insensitive substring of the entity ID, "
                            "e.g. 'sensor.pool' or 'light.'."
                        ),
                    },
                    "area_filter": {
                        "type": "string",
                        "description": (
                            "Case-insensitive substring of the area name, "
                            "e.g. 'living room' or 'garage'."
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "description": (
                            f"Maximum number of entities to return. Defaults to "
                            f"{DEFAULT_ENTITY_RESULTS}, maximum {MAX_ENTITY_RESULTS}."
                        ),
                        "default": DEFAULT_ENTITY_RESULTS,
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
                "  1. Call with `domain='light'` (or whatever domain you need) "
                "to see its actions.\n"
                "  2. Narrow with `action_filter` (e.g. 'turn_on') until "
                f"{ACTION_FIELD_DETAIL_LIMIT} actions or fewer match: each "
                "is then listed with its fields (name, whether required, "
                "description, example and HA selector) and target, which is "
                "what calling it needs.\n\n"
                "Each entry gives `domain.action` (service id), its "
                "description, and whether it supports `return_response=true`. "
                "Calling with no arguments lists every action by name only."
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
    {
        "type": "function",
        "function": {
            "name": GET_HOME_STATUS_TOOL_NAME,
            "description": (
                "Gets an overview of the household's current state, curated by the "
                "operator -- for example who is home, energy prices and the state of "
                "key devices. Call it whenever the current state of the home matters "
                "to the request, and to learn which entities the household cares "
                "about. For anything the overview does not cover, use the other "
                "Home Assistant tools (entity lists, templates, history).\n\n"
                "Returns: the overview as text, as rendered by Home Assistant just "
                "now. On errors, returns a descriptive error message."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]


def _home_assistant_client(
    exec_context: ToolExecutionContext,
) -> HomeAssistantClientWrapper | None:
    """The client of the Home Assistant instance the turn's profile selected."""
    if exec_context.plugins is None:
        return None
    instance = exec_context.plugins.get(HomeAssistantInstance)
    return None if instance is None else instance.client


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
    ha_client = _home_assistant_client(exec_context)
    if ha_client is None:
        logger.error("Home Assistant client not available in execution context")
        return "Error: Home Assistant integration is not configured or available."

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


async def get_home_status_tool(exec_context: ToolExecutionContext) -> ToolResult:
    """Render the instance's operator-configured context template."""
    instance = (
        None
        if exec_context.plugins is None
        else exec_context.plugins.get(HomeAssistantInstance)
    )
    if instance is None:
        logger.error("Home Assistant client not available in execution context")
        return ToolResult(
            text="Error: Home Assistant integration is not configured or available."
        )
    if not instance.context_template:
        return ToolResult(
            text=(
                "Error: No home status overview is configured for this Home "
                "Assistant instance."
            )
        )

    try:
        rendered = await instance.client.async_get_rendered_template(
            template=instance.context_template
        )
    except HomeassistantAPIError as e:
        logger.exception(f"Home Assistant API error rendering home status: {e}")
        return ToolResult(text=f"Error: Home Assistant API error - {e!s}")
    except Exception as e:
        logger.exception(f"Unexpected error rendering home status: {e}")
        return ToolResult(text=f"Error: Failed to render home status - {e!s}")

    if not rendered or not rendered.strip():
        return ToolResult(text="The home status overview rendered empty.")
    return ToolResult(text=rendered.strip())


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
    ha_client = _home_assistant_client(exec_context)
    if ha_client is None:
        logger.error("Home Assistant client not available in execution context")
        return ToolResult(
            text="Error: Home Assistant integration is not configured or available."
        )

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
        logger.exception(
            "Camera snapshot failed for %s (%s): %s",
            camera_entity_id,
            type(e).__name__,
            e,
        )
        if isinstance(e, CameraSnapshotTimeoutError | CameraSnapshotHTTPError):
            detail = str(e)
        else:
            # A bare TimeoutError or connection error can stringify to "".
            detail = f"{camera_entity_id}: {type(e).__name__}"
            if str(e):
                detail += f": {e}"
        return ToolResult(text=f"Error: Failed to retrieve camera snapshot: {detail}")

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
    ha_client = _home_assistant_client(exec_context)
    if ha_client is None:
        logger.error("Home Assistant client not available in execution context")
        return ToolResult(
            text="Error: Home Assistant integration is not configured or available."
        )

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

    ha_client = _home_assistant_client(exec_context)
    if ha_client is None:
        logger.error("Home Assistant client not available in execution context")
        return ToolResult(
            text="Error: Home Assistant integration is not configured or available."
        )

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

    ha_client = _home_assistant_client(exec_context)
    if ha_client is None:
        logger.error("Home Assistant client not available in execution context")
        return ToolResult(
            text="Error: Home Assistant integration is not configured or available."
        )

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
        with_fields = total_matches <= ACTION_FIELD_DETAIL_LIMIT
        lines = [header + ":"]
        for entry in result_entries:
            line = f"- {entry['domain']}.{entry['action']}"
            description = entry.get("description")
            if description:
                line += f" — {description}"
            if entry.get("supports_response"):
                line += " [supports response]"
            lines.append(line)
            if with_fields:
                lines.extend(_action_detail_lines(entry))
        if not with_fields:
            lines.append(
                f"Field details are shown for {ACTION_FIELD_DETAIL_LIMIT} actions "
                "or fewer: narrow with domain and action_filter to see them."
            )
        text = "\n".join(lines)

    return ToolResult(text=text, data=result_data)


def _action_detail_lines(entry: ActionCatalogEntry) -> list[str]:
    """One line per field of ``entry``, plus its target, for the model to call it by."""
    lines: list[str] = []
    if entry.get("target"):
        lines.append(f"    target: {_compact(entry['target'])}")
    for field_name, field in entry["fields"].items():
        line = f"    {field_name}"
        if field.get("required"):
            line += " (required)"
        if description := field.get("description"):
            line += f": {description}"
        if "example" in field:
            line += f" e.g. {_compact(field['example'])}"
        if selector := field.get("selector"):
            line += f" [{_compact(selector)}]"
        lines.append(line)
    return lines


def _compact(value: object) -> str:
    rendered = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    if len(rendered) > ACTION_DETAIL_VALUE_CHARS:
        return rendered[: ACTION_DETAIL_VALUE_CHARS - 1] + "…"
    return rendered


async def list_home_assistant_entities_tool(
    exec_context: ToolExecutionContext,
    query: str | None = None,
    entity_id_filter: str | None = None,
    area_filter: str | None = None,
    max_results: int = DEFAULT_ENTITY_RESULTS,
) -> ToolResult:
    """Find entities by query and filters, with each one's current state."""
    logger.info(
        "Listing HA entities: query=%r, entity_filter=%r, area_filter=%r, max=%d",
        query,
        entity_id_filter,
        area_filter,
        max_results,
    )
    ha_client = _home_assistant_client(exec_context)
    if ha_client is None:
        logger.error("Home Assistant client not available in execution context")
        return ToolResult(
            text="Error: Home Assistant integration is not configured or available."
        )
    query_words = _words(query or "")
    if not (query_words or entity_id_filter or area_filter):
        return ToolResult(
            text=(
                "Error: give a query (e.g. 'pool temperature'), an "
                "entity_id_filter or an area_filter. Home Assistant has too "
                "many entities to list them all."
            )
        )
    max_results = max(1, min(max_results, MAX_ENTITY_RESULTS))

    try:
        entities = await ha_client.async_get_entity_list_with_metadata()
    except Exception as e:
        logger.exception(f"Error listing entities: {e}")
        return ToolResult(text=f"Error: Failed to list entities: {e!s}")

    matches = find_entities(
        entities,
        query_words=query_words,
        entity_id_filter=entity_id_filter,
        area_filter=area_filter,
    )
    total_matches = len(matches)
    shown = matches[:max_results]

    try:
        states = await ha_client.async_get_current_states([
            entity["entity_id"] for entity in shown
        ])
    except HomeassistantAPIError as e:
        logger.exception(f"Error reading entity states: {e}")
        return ToolResult(text=f"Error: Failed to read entity states: {e!s}")

    result_entities = [
        EntityListing(
            **entity,
            state=states[entity["entity_id"]]["state"],
            unit=states[entity["entity_id"]]["unit"],
        )
        for entity in shown
    ]
    return ToolResult(
        text=_render_entity_listing(result_entities, total_matches),
        data={"entities": result_entities, "total_matches": total_matches},
    )


class EntityListing(EntityMetadata):
    """One entity in a listing, with the state it reported just now."""

    state: str
    unit: str | None


def _words(text: str) -> list[str]:
    # Letters and digits in any script; underscores split entity IDs.
    return re.findall(r"[^\W_]+", text.lower())


def _word_matches(query_word: str, entity_words: set[str]) -> bool:
    if query_word in entity_words:
        return True
    if len(query_word) < MIN_PREFIX_MATCH_LENGTH:
        return False
    return any(
        len(word) >= MIN_PREFIX_MATCH_LENGTH
        and (word.startswith(query_word) or query_word.startswith(word))
        for word in entity_words
    )


def find_entities(
    entities: list[EntityMetadata],
    *,
    query_words: list[str],
    entity_id_filter: str | None,
    area_filter: str | None,
) -> list[EntityMetadata]:
    """Filter ``entities`` and rank them by how many query words they match.

    The filters are substring tests and must all hold. Query words are
    matched against every descriptive field at once, so "pool temperature"
    finds a sensor named "Temp inflow" in the Pool area with a temperature
    device class. An entity matching no query word is dropped; among the rest,
    more matched words rank first and ties keep Home Assistant's order.
    """
    id_filter = (entity_id_filter or "").lower()
    area = (area_filter or "").lower()
    scored: list[tuple[int, int, EntityMetadata]] = []
    for position, entity in enumerate(entities):
        if id_filter and id_filter not in entity["entity_id"].lower():
            continue
        if area and area not in (entity.get("area_name") or "").lower():
            continue
        score = 0
        if query_words:
            entity_words = set(
                _words(
                    " ".join(
                        value or ""
                        for value in (
                            entity["entity_id"],
                            entity.get("name"),
                            entity.get("area_name"),
                            entity.get("device_name"),
                            entity.get("device_class"),
                        )
                    )
                )
            )
            score = sum(_word_matches(word, entity_words) for word in query_words)
            if score == 0:
                continue
        scored.append((-score, position, entity))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [entity for _, _, entity in scored]


def _render_entity_listing(entities: list[EntityListing], total_matches: int) -> str:
    if total_matches == 0:
        return "No matching entities found."
    if total_matches <= len(entities):
        lines = [f"Found {total_matches} matching entities:", ""]
    else:
        lines = [
            f"Found {total_matches} matching entities, best {len(entities)} "
            "shown. Narrow the query for the rest:",
            "",
        ]
    for entity in entities:
        line = f"- {entity['entity_id']}"
        if entity["name"] and entity["name"] != entity["entity_id"]:
            line += f" - {entity['name']}"
        if area := entity["area_name"]:
            line += f" (Area: {area})"
        if device := entity["device_name"]:
            line += f" [Device: {device}]"
        unit = entity["unit"]
        line += f": {entity['state']} {unit}" if unit else f": {entity['state']}"
        lines.append(line)
    return "\n".join(lines)


def _tool(
    name: str, implementation: ToolImplementation, *tags: ToolTag
) -> ToolRegistration:
    definitions = {
        definition["function"]["name"]: definition
        for definition in HOME_ASSISTANT_TOOLS_DEFINITION
    }
    return ToolRegistration(
        definition=definitions[name],
        implementation=implementation,
        metadata=make_local_tool_metadata(tags),
    )


HOME_ASSISTANT_TOOLS: tuple[ToolRegistration, ...] = (
    _tool(
        "download_state_history",
        download_state_history_tool,
        ToolTag.SCRIPT_DETERMINISTIC,
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.HOME_AUTOMATION,
        ToolTag.DATA,
        ToolTag.OUTPUT_TRUSTED,
    ),
    _tool(
        "render_home_assistant_template",
        render_home_assistant_template_tool,
        ToolTag.SCRIPT_DETERMINISTIC,
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.HOME_AUTOMATION,
        ToolTag.DATA,
        ToolTag.OUTPUT_TRUSTED,
    ),
    _tool(
        GET_HOME_STATUS_TOOL_NAME,
        get_home_status_tool,
        ToolTag.SCRIPT_DETERMINISTIC,
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.HOME_AUTOMATION,
        ToolTag.DATA,
        ToolTag.OUTPUT_TRUSTED,
    ),
    _tool(
        "get_camera_snapshot",
        get_camera_snapshot_tool,
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.HOME_AUTOMATION,
        ToolTag.CAMERA,
        ToolTag.MEDIA,
        ToolTag.OUTPUT_TRUSTED,
    ),
    _tool(
        "list_home_assistant_entities",
        list_home_assistant_entities_tool,
        ToolTag.SCRIPT_DETERMINISTIC,
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.HOME_AUTOMATION,
        ToolTag.OUTPUT_TRUSTED,
    ),
    _tool(
        "list_home_assistant_actions",
        list_home_assistant_actions_tool,
        ToolTag.SCRIPT_DETERMINISTIC,
        ToolTag.READ_ONLY,
        ToolTag.HOME_AUTOMATION,
        ToolTag.OUTPUT_TRUSTED,
    ),
    _tool(
        "call_home_assistant_action",
        call_home_assistant_action_tool,
        ToolTag.SCRIPT_DETERMINISTIC,
        ToolTag.STATE_CHANGING,
        ToolTag.HOME_AUTOMATION,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_TRUSTED,
    ),
)
