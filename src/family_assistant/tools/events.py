"""
Event listener system tools.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from family_assistant.storage.context import get_db_context
from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


# Tool definitions
EVENT_TOOLS_DEFINITION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "query_recent_events",
            "description": (
                "Query recent events from the event system. Returns raw event data "
                "in JSON format for examining event structure and content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source_id": {
                        "type": "string",
                        "description": (
                            "Optional. Filter by event source "
                            "(e.g., 'home_assistant', 'indexing', 'webhook')"
                        ),
                    },
                    "hours": {
                        "type": "integer",
                        "description": "Number of hours to look back (default: 1, max: 48)",
                        "default": 1,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of events to return (default: 10, max: 20)",
                        "default": 10,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "test_event_listener",
            "description": (
                "Test what events would match given listener conditions. "
                "Use this before creating a listener to ensure the match conditions are correct. "
                "Returns events that would have triggered the listener."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source_id": {
                        "type": "string",
                        "description": "Event source to test against (e.g., 'home_assistant', 'indexing')",
                    },
                    "match_conditions": {
                        "type": "object",
                        "description": (
                            "Match conditions to test. Use dot notation for nested fields "
                            "(e.g., {'entity_id': 'person.andrew', 'new_state.state': 'Home'})"
                        ),
                    },
                    "hours": {
                        "type": "integer",
                        "description": "Number of hours to look back (default: 24, max: 48)",
                        "default": 24,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of matching events to return (default: 10, max: 20)",
                        "default": 10,
                    },
                },
                "required": ["source_id", "match_conditions"],
            },
        },
    },
]


async def query_recent_events_tool(
    exec_context: ToolExecutionContext,
    source_id: str | None = None,
    hours: int = 1,
    limit: int = 10,
) -> str:
    """
    Query recent events from the event system.

    Args:
        exec_context: Tool execution context
        source_id: Optional filter by event source
        hours: Number of hours to look back (default 1, max 48)
        limit: Maximum number of events to return (default 10, max 20)

    Returns:
        JSON string containing raw event data
    """
    # Validate parameters
    hours = min(max(hours, 1), 48)  # Clamp between 1 and 48
    limit = min(max(limit, 1), 20)  # Clamp between 1 and 20

    try:
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours)

        async with get_db_context() as db_ctx:
            # Build query
            if source_id:
                query = text("""
                    SELECT event_id, source_id, event_data, triggered_listener_ids, timestamp
                    FROM recent_events
                    WHERE source_id = :source_id AND timestamp >= :cutoff_time
                    ORDER BY timestamp DESC
                    LIMIT :limit
                """)
                params = {
                    "source_id": source_id,
                    "cutoff_time": cutoff_time,
                    "limit": limit,
                }
            else:
                query = text("""
                    SELECT event_id, source_id, event_data, triggered_listener_ids, timestamp
                    FROM recent_events
                    WHERE timestamp >= :cutoff_time
                    ORDER BY timestamp DESC
                    LIMIT :limit
                """)
                params = {"cutoff_time": cutoff_time, "limit": limit}

            result = await db_ctx.fetch_all(query, params)

        if not result:
            return json.dumps({
                "events": [],
                "message": f"No events found in the last {hours} hours",
            })

        # Collect raw events
        events = []
        for row in result:
            # Parse event data - handle both string and dict (some DB drivers auto-parse JSON)
            event_data = row["event_data"]
            if isinstance(event_data, str):
                try:
                    event_data = json.loads(event_data)
                except json.JSONDecodeError:
                    event_data = {"error": "Invalid JSON", "raw": event_data}

            # Parse triggered listeners - handle both string and list
            triggered_listeners = row["triggered_listener_ids"]
            if triggered_listeners is None:
                triggered_listeners = []
            elif isinstance(triggered_listeners, str):
                try:
                    triggered_listeners = json.loads(triggered_listeners)
                except json.JSONDecodeError:
                    triggered_listeners = []

            # Handle timestamp format (SQLite returns strings)
            timestamp = row["timestamp"]
            if isinstance(timestamp, str):
                timestamp_str = timestamp
            else:
                timestamp_str = timestamp.isoformat()

            events.append({
                "event_id": row["event_id"],
                "source_id": row["source_id"],
                "timestamp": timestamp_str,
                "event_data": event_data,
                "triggered_listeners": triggered_listeners,
            })

        # Return raw JSON events
        return json.dumps(
            {
                "events": events,
                "count": len(events),
                "hours_queried": hours,
                "source_filter": source_id,
            },
            indent=2,
        )

    except Exception as e:
        logger.error(f"Error querying recent events: {e}", exc_info=True)
        return f"Error: Failed to query recent events. {str(e)}"


async def test_event_listener_tool(
    exec_context: ToolExecutionContext,
    source_id: str,
    match_conditions: dict[str, Any],
    hours: int = 24,
    limit: int = 10,
) -> str:
    """
    Test what events would match given listener conditions.

    Args:
        exec_context: Tool execution context
        source_id: Event source to test against
        match_conditions: Match conditions to test
        hours: Number of hours to look back (default 24, max 48)
        limit: Maximum number of matching events to return (default 10, max 20)

    Returns:
        JSON string containing matched events and analysis
    """
    # Validate parameters
    hours = min(max(hours, 1), 48)  # Clamp between 1 and 48
    limit = min(max(limit, 1), 20)  # Clamp between 1 and 20

    if not match_conditions:
        return json.dumps({
            "error": "match_conditions cannot be empty",
            "message": "Please provide at least one condition to test",
        })

    try:
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours)

        async with get_db_context() as db_ctx:
            # Query all events for the source within the time range
            query = text("""
                SELECT event_id, source_id, event_data, timestamp
                FROM recent_events
                WHERE source_id = :source_id AND timestamp >= :cutoff_time
                ORDER BY timestamp DESC
            """)
            params = {
                "source_id": source_id,
                "cutoff_time": cutoff_time,
            }

            result = await db_ctx.fetch_all(query, params)

        if not result:
            return json.dumps({
                "matched_events": [],
                "total_tested": 0,
                "message": f"No events found for source '{source_id}' in the last {hours} hours",
                "match_conditions": match_conditions,
            })

        # Test each event against the match conditions
        matched_events = []
        total_tested = 0

        for row in result:
            total_tested += 1

            # Parse event data - handle both string and dict (some DB drivers auto-parse JSON)
            event_data = row["event_data"]
            if isinstance(event_data, str):
                try:
                    event_data = json.loads(event_data)
                except json.JSONDecodeError:
                    continue
            elif not isinstance(event_data, dict):
                continue

            # Check if event matches conditions
            if _check_match_conditions(event_data, match_conditions):
                # Handle timestamp format
                timestamp = row["timestamp"]
                if isinstance(timestamp, str):
                    timestamp_str = timestamp
                else:
                    timestamp_str = timestamp.isoformat()

                matched_events.append({
                    "event_id": row["event_id"],
                    "timestamp": timestamp_str,
                    "event_data": event_data,
                })

                # Limit matched events
                if len(matched_events) >= limit:
                    break

        # Analyze why events might not have matched
        analysis = []
        if total_tested > 0 and len(matched_events) == 0:
            # Get a sample event to show what fields are available
            try:
                # Handle both string and dict (some DB drivers auto-parse JSON)
                sample_data = result[0]["event_data"]
                if isinstance(sample_data, str):
                    sample_data = json.loads(sample_data)
                elif not isinstance(sample_data, dict):
                    sample_data = None

                if sample_data:
                    analysis.append("No events matched your conditions.")
                    analysis.append(
                        f"Sample event structure: {_get_event_structure(sample_data)}"
                    )

                    # Check if any condition keys exist in the data
                    for key, expected_value in match_conditions.items():
                        actual_value = _get_nested_value(sample_data, key)
                        if actual_value is None:
                            analysis.append(f"Field '{key}' not found in events")
                        elif actual_value != expected_value:
                            analysis.append(
                                f"Field '{key}' exists but has value: {repr(actual_value)}"
                            )
            except Exception:
                pass

        return json.dumps(
            {
                "matched_events": matched_events,
                "total_tested": total_tested,
                "matched_count": len(matched_events),
                "match_conditions": match_conditions,
                "hours_queried": hours,
                "analysis": analysis if analysis else None,
            },
            indent=2,
        )

    except Exception as e:
        logger.error(f"Error testing event listener: {e}", exc_info=True)
        return json.dumps({
            "error": f"Failed to test event listener: {str(e)}",
            "match_conditions": match_conditions,
        })


def _check_match_conditions(event_data: dict, match_conditions: dict | None) -> bool:
    """Check if event matches the listener's conditions using simple dict equality."""
    if not match_conditions:
        return True  # No conditions means match all events

    for key, expected_value in match_conditions.items():
        actual_value = _get_nested_value(event_data, key)
        if actual_value != expected_value:
            return False
    return True


def _get_nested_value(data: dict, key_path: str) -> Any:
    """Get value from nested dict using dot notation (e.g., 'new_state.state')."""
    keys = key_path.split(".")
    value = data
    for key in keys:
        if isinstance(value, dict) and key in value:
            value = value[key]
        else:
            return None
    return value


def _get_event_structure(
    data: dict, max_depth: int = 3, current_depth: int = 0
) -> dict | str:
    """Get a simplified structure of the event data for debugging."""
    if current_depth >= max_depth:
        return "..."

    structure = {}
    for key, value in data.items():
        if isinstance(value, dict):
            structure[key] = _get_event_structure(value, max_depth, current_depth + 1)
        elif isinstance(value, list):
            structure[key] = f"[{len(value)} items]" if value else "[]"
        else:
            structure[key] = type(value).__name__

    return structure
