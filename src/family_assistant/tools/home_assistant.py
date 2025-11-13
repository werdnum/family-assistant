"""Home Assistant integration tools.

This module contains tools for interacting with Home Assistant, including
rendering templates and retrieving camera snapshots.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from family_assistant.tools.types import (
    ToolAttachment,
    ToolResult,
    get_attachment_limits,
)

if TYPE_CHECKING:
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
# ast-grep-ignore: no-dict-any - Legacy code - needs structured types
HOME_ASSISTANT_TOOLS_DEFINITION: list[dict[str, Any]] = [
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
        logger.error(f"Home Assistant API error rendering template: {e}", exc_info=True)
        return f"Error: Home Assistant API error - {str(e)}"
    except Exception as e:
        logger.error(f"Unexpected error rendering template: {e}", exc_info=True)
        return f"Error: Failed to render template - {str(e)}"


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

            # Filter for camera entities
            cameras = []
            for entity in states:
                if entity.entity_id.startswith("camera."):
                    # Get friendly name if available
                    friendly_name = entity.attributes.get(
                        "friendly_name", entity.entity_id
                    )
                    if friendly_name and friendly_name != entity.entity_id:
                        cameras.append(f"- {entity.entity_id} ({friendly_name})")
                    else:
                        cameras.append(f"- {entity.entity_id}")

            if not cameras:
                return ToolResult(text="No camera entities found in Home Assistant.")

            return ToolResult(
                text="Available cameras in Home Assistant:\n" + "\n".join(cameras)
            )

        except Exception as e:
            logger.error(f"Error listing cameras: {e}", exc_info=True)
            return ToolResult(text=f"Error listing available cameras: {str(e)}")

    # Use the HA client's custom camera snapshot method to get raw binary data
    try:
        image_content = await ha_client.async_get_camera_snapshot(camera_entity_id)

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

    except Exception as e:
        logger.error(f"Error getting camera snapshot: {e}", exc_info=True)
        return ToolResult(text=f"Error: Failed to retrieve camera snapshot: {str(e)}")


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

    # Parse timestamps
    try:
        # Parse end_time first to determine default start_time
        if end_time:
            end_timestamp = datetime.fromisoformat(
                end_time.replace("Z", "+00:00")
            ).astimezone(UTC)
        else:
            # Default to now
            end_timestamp = datetime.now(UTC)

        if start_time:
            start_timestamp = datetime.fromisoformat(
                start_time.replace("Z", "+00:00")
            ).astimezone(UTC)
        else:
            # Default to 24 hours before end_time
            start_timestamp = end_timestamp - timedelta(days=1)

        # Validate that start_time is before end_time
        if start_timestamp >= end_timestamp:
            return ToolResult(
                text=f"Error: start_time ({start_timestamp.isoformat()}) must be before end_time ({end_timestamp.isoformat()})"
            )

    except (ValueError, AttributeError) as e:
        logger.error(f"Error parsing timestamps: {e}", exc_info=True)
        return ToolResult(
            text=f"Error: Invalid timestamp format. Use ISO 8601 format (e.g., '2024-01-01T00:00:00Z'): {str(e)}"
        )

    # Retrieve history
    try:
        # Build parameters
        histories = []

        # If specific entity IDs are requested, fetch State objects in bulk
        states = None
        if entity_ids:
            # Fetch all states in one bulk call instead of looping
            all_states = await ha_client.async_get_states()

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

        if not histories:
            return ToolResult(
                text="No history data found for the specified parameters."
            )

        # Convert to JSON
        json_data = json.dumps(
            {
                "start_time": start_timestamp.isoformat(),
                "end_time": end_timestamp.isoformat(),
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
            f"from {start_timestamp.strftime('%Y-%m-%d %H:%M:%S')} to {end_timestamp.strftime('%Y-%m-%d %H:%M:%S')}"
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

    except Exception as e:
        logger.error(f"Error retrieving state history: {e}", exc_info=True)
        return ToolResult(text=f"Error: Failed to retrieve state history: {str(e)}")


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
                if (area_name := e.get("area_name"))
                and filter_lower in area_name.lower()
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

    except Exception as e:
        logger.error(f"Error listing entities: {e}", exc_info=True)
        return ToolResult(text=f"Error: Failed to list entities: {str(e)}")
