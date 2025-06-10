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
            # Parse event data
            try:
                event_data = json.loads(row["event_data"])
            except json.JSONDecodeError:
                event_data = {"error": "Invalid JSON", "raw": row["event_data"]}

            # Parse triggered listeners
            try:
                triggered_listeners = (
                    json.loads(row["triggered_listener_ids"])
                    if row["triggered_listener_ids"]
                    else []
                )
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
