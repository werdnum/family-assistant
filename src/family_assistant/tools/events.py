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
                "Query recent events from the event system for debugging. "
                "Shows what events have been captured by the system."
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
                        "description": "Number of hours to look back (default: 24, max: 48)",
                        "default": 24,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of events to return (default: 50)",
                        "default": 50,
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
    hours: int = 24,
    limit: int = 50,
) -> str:
    """
    Query recent events from the event system.

    Args:
        exec_context: Tool execution context
        source_id: Optional filter by event source
        hours: Number of hours to look back (max 48)
        limit: Maximum number of events to return

    Returns:
        Formatted string with event information
    """
    # Validate parameters
    hours = min(max(hours, 1), 48)  # Clamp between 1 and 48
    limit = min(max(limit, 1), 100)  # Clamp between 1 and 100

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
            return f"No events found in the last {hours} hours"

        # Format results
        events_by_source: dict[str, list[dict]] = {}
        for row in result:
            source = row["source_id"]
            if source not in events_by_source:
                events_by_source[source] = []

            # Parse event data
            try:
                event_data = json.loads(row["event_data"])
            except json.JSONDecodeError:
                event_data = {"error": "Invalid JSON"}

            # Parse triggered listeners
            try:
                triggered = (
                    json.loads(row["triggered_listener_ids"])
                    if row["triggered_listener_ids"]
                    else []
                )
            except json.JSONDecodeError:
                triggered = []

            events_by_source[source].append({
                "timestamp": row["timestamp"],
                "entity_id": event_data.get("entity_id", "Unknown"),
                "event_data": event_data,
                "triggered_listeners": triggered,
            })

        # Format output
        output_lines = [f"Recent events from the last {hours} hours:"]

        for source, events in events_by_source.items():
            output_lines.append(f"\n{source.upper()} ({len(events)} events):")

            for event in events[:10]:  # Show max 10 per source
                # Handle both datetime objects and string timestamps (SQLite returns strings)
                timestamp = event["timestamp"]
                if isinstance(timestamp, str):
                    # Parse ISO format timestamp from SQLite
                    try:
                        timestamp = datetime.fromisoformat(
                            timestamp.replace("Z", "+00:00")
                        )
                        timestamp_str = timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
                    except ValueError:
                        timestamp_str = str(timestamp)
                else:
                    timestamp_str = timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
                entity = event["entity_id"]
                triggered = len(event["triggered_listeners"])

                output_lines.append(f"  - {timestamp_str}: {entity}")

                # Show key event details based on source
                if source == "home_assistant" and "new_state" in event["event_data"]:
                    new_state = event["event_data"]["new_state"]
                    if new_state and "state" in new_state:
                        old_state = event["event_data"].get("old_state", {})
                        old_state_val = (
                            old_state.get("state") if old_state else "unknown"
                        )
                        output_lines.append(
                            f"    State: {old_state_val} → {new_state['state']}"
                        )

                if triggered > 0:
                    output_lines.append(f"    Triggered {triggered} listener(s)")

            if len(events) > 10:
                output_lines.append(f"  ... and {len(events) - 10} more")

        output_lines.append(f"\nTotal events: {len(result)}")
        return "\n".join(output_lines)

    except Exception as e:
        logger.error(f"Error querying recent events: {e}", exc_info=True)
        return f"Error: Failed to query recent events. {str(e)}"
