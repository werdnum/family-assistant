"""
Event processor that routes events to storage and listeners.
"""

import json
import logging
import time
from typing import Any

from sqlalchemy import text

from family_assistant.events.sources import EventSource
from family_assistant.events.storage import EventStorage
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.storage.events import check_and_update_rate_limit

logger = logging.getLogger(__name__)


class EventProcessor:
    """Routes events from sources to storage and listeners."""

    def __init__(
        self,
        sources: dict[str, EventSource],
        db_context: DatabaseContext | None = None,
        sample_interval_hours: float = 1.0,
    ) -> None:
        """
        Initialize event processor.

        Args:
            sources: Dictionary of source_id -> EventSource instances
            db_context: Database context (optional, will create if needed)
            sample_interval_hours: Hours between storing event samples
        """
        self.sources = sources
        self.event_storage = EventStorage(sample_interval_hours)
        # Cache listeners by source_id for efficient lookup
        self._listener_cache: dict[str, list[dict]] = {}
        self._cache_refresh_interval = 60  # Refresh from DB every minute
        self._last_cache_refresh = 0
        self._running = False

    async def start(self) -> None:
        """Start all event sources."""
        self._running = True
        logger.info(f"Starting EventProcessor with {len(self.sources)} sources")

        # Refresh listener cache
        await self._refresh_listener_cache()

        # Start all sources
        for source_id, source in self.sources.items():
            try:
                await source.start(self)
                logger.info(f"Started event source: {source_id}")
            except Exception as e:
                logger.error(
                    f"Failed to start event source {source_id}: {e}", exc_info=True
                )

    async def stop(self) -> None:
        """Stop all event sources."""
        self._running = False
        logger.info("Stopping EventProcessor")

        for source_id, source in self.sources.items():
            try:
                await source.stop()
                logger.info(f"Stopped event source: {source_id}")
            except Exception as e:
                logger.error(
                    f"Failed to stop event source {source_id}: {e}", exc_info=True
                )

    async def process_event(self, source_id: str, event_data: dict[str, Any]) -> None:
        """Process an event from a source."""
        if not self._running:
            return

        # Refresh cache if needed
        if time.time() - self._last_cache_refresh > self._cache_refresh_interval:
            await self._refresh_listener_cache()

        # Get all active listeners for this source
        listeners = self._listener_cache.get(source_id, [])

        # Evaluate match conditions for relevant listeners
        triggered_listener_ids = []
        for listener in listeners:
            if self._check_match_conditions(event_data, listener["match_conditions"]):
                # Check and update rate limit atomically
                async with get_db_context() as db_ctx:
                    allowed, reason = await check_and_update_rate_limit(
                        db_ctx, listener
                    )
                    if allowed:
                        # For now, just log - actual action execution will come later
                        logger.info(
                            f"Listener {listener['id']} ({listener['name']}) triggered by event"
                        )
                        triggered_listener_ids.append(listener["id"])

                        # Handle one-time listeners
                        if listener.get("one_time"):
                            await self._disable_listener(listener["id"])
                    else:
                        logger.warning(
                            f"Listener {listener['id']} rate limited: {reason}"
                        )

        # Store event for debugging/testing
        async with get_db_context() as db_ctx:
            await self.event_storage.store_event(
                source_id, event_data, triggered_listener_ids
            )

    def _check_match_conditions(
        self, event_data: dict, match_conditions: dict | None
    ) -> bool:
        """Check if event matches the listener's conditions using simple dict equality."""
        if not match_conditions:
            return True  # No conditions means match all events

        for key, expected_value in match_conditions.items():
            actual_value = self._get_nested_value(event_data, key)
            if actual_value != expected_value:
                return False
        return True

    def _get_nested_value(self, data: dict, key_path: str) -> Any:
        """Get value from nested dict using dot notation (e.g., 'new_state.state')."""
        keys = key_path.split(".")
        value = data
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return None
        return value

    async def _refresh_listener_cache(self) -> None:
        """Refresh the listener cache from database."""
        async with get_db_context() as db_ctx:
            result = await db_ctx.fetch_all(
                text("SELECT * FROM event_listeners WHERE enabled = TRUE")
            )

            new_cache = {}
            for row in result:
                listener_dict = dict(row)
                # Parse JSON fields
                listener_dict["match_conditions"] = json.loads(
                    listener_dict.get("match_conditions") or "{}"
                )
                listener_dict["action_config"] = json.loads(
                    listener_dict.get("action_config") or "{}"
                )

                source_id = listener_dict["source_id"]
                if source_id not in new_cache:
                    new_cache[source_id] = []
                new_cache[source_id].append(listener_dict)

            self._listener_cache = new_cache
            self._last_cache_refresh = time.time()
            logger.info(
                f"Refreshed listener cache: {sum(len(v) for v in new_cache.values())} "
                f"listeners across {len(new_cache)} sources"
            )

    async def _disable_listener(self, listener_id: int) -> None:
        """Disable a one-time listener after it triggers."""
        async with get_db_context() as db_ctx:
            await db_ctx.execute_with_retry(
                text("UPDATE event_listeners SET enabled = FALSE WHERE id = :id"),
                {"id": listener_id},
            )
            logger.info(f"Disabled one-time listener {listener_id}")
