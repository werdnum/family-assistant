"""
Event storage management with sampling strategy.
"""

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.events import EventSourceType

logger = logging.getLogger(__name__)


class EventStorage:
    """Manages storage of events with sampling strategy."""

    def __init__(
        self,
        sample_interval_hours: float = 1.0,
        get_db_context_func: Callable[[], DatabaseContext] | None = None,
    ) -> None:
        """
        Initialize event storage.

        Args:
            sample_interval_hours: Hours between storing samples (default: 1 hour)
            get_db_context_func: Function to get database context with engine
        """
        self.sample_interval_seconds = sample_interval_hours * 3600
        self.last_stored: dict[str, float] = {}  # key -> timestamp for sampling
        self.max_event_size = 100000  # 100KB max event size
        self.get_db_context_func = get_db_context_func

    async def store_event(
        self,
        source_id: EventSourceType | str,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        event_data: dict[str, Any],
        triggered_listener_ids: list[int] | None = None,
    ) -> None:
        """Store event if it should be stored based on sampling rules (opens new DB context)."""
        if self.get_db_context_func:
            async with self.get_db_context_func() as db_ctx:
                await self.store_event_in_context(
                    db_ctx, source_id, event_data, triggered_listener_ids
                )
        else:
            raise RuntimeError(
                "EventStorage requires get_db_context_func to be provided"
            )

    async def store_event_in_context(
        self,
        db_ctx: DatabaseContext,
        source_id: EventSourceType | str,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        event_data: dict[str, Any],
        triggered_listener_ids: list[int] | None = None,
    ) -> None:
        """Store event if it should be stored based on sampling rules within existing DB context."""
        now = time.time()

        # Convert enum to string if needed
        source_str = (
            source_id.value if isinstance(source_id, EventSourceType) else source_id
        )

        # Check event size
        event_json = json.dumps(event_data)
        if len(event_json) > self.max_event_size:
            logger.warning(
                f"Skipping event from {source_str} - too large ({len(event_json)} bytes)"
            )
            return

        # Create sampling key based on source and entity_id if present
        entity_id = event_data.get("entity_id", "unknown")
        key = f"{source_str}:{entity_id}"

        # Always store if it triggered listeners
        if triggered_listener_ids:
            await self._write_event_in_context(
                db_ctx, source_str, event_data, triggered_listener_ids
            )
            return

        # Sample storage: 1 per entity per hour
        last = self.last_stored.get(key, 0)
        if now - last > self.sample_interval_seconds:
            self.last_stored[key] = now
            await self._write_event_in_context(db_ctx, source_str, event_data, None)

    async def _write_event(
        self,
        source_id: str,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        event_data: dict[str, Any],
        triggered_listener_ids: list[int] | None,
    ) -> None:
        """Write event to database (opens new DB context)."""
        if self.get_db_context_func:
            async with self.get_db_context_func() as db_ctx:
                await self._write_event_in_context(
                    db_ctx, source_id, event_data, triggered_listener_ids
                )
        else:
            raise RuntimeError(
                "EventStorage requires get_db_context_func to be provided"
            )

    async def _write_event_in_context(
        self,
        db_ctx: DatabaseContext,
        source_id: str,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        event_data: dict[str, Any],
        triggered_listener_ids: list[int] | None,
    ) -> None:
        """Write event to database within existing DB context."""
        try:
            # Generate unique event ID
            event_id = f"{source_id}:{int(time.time() * 1000000)}"

            await db_ctx.execute_with_retry(
                text("""INSERT INTO recent_events 
                   (event_id, source_id, event_data, triggered_listener_ids, timestamp)
                   VALUES (:event_id, :source_id, :event_data, :triggered_listener_ids, :timestamp)"""),
                {
                    "event_id": event_id,
                    "source_id": source_id,
                    "event_data": json.dumps(event_data),
                    "triggered_listener_ids": json.dumps(triggered_listener_ids)
                    if triggered_listener_ids
                    else None,
                    "timestamp": datetime.now(UTC),
                },
            )
            logger.debug(
                f"Stored event {event_id} (triggered: {len(triggered_listener_ids or [])} listeners)"
            )
        except Exception as e:
            logger.error(f"Failed to store event: {e}", exc_info=True)
            # Don't fail event processing due to storage errors
