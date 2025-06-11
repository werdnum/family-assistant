"""
Indexing event source implementation.
"""

import asyncio
import contextlib
import logging
from enum import Enum
from typing import TYPE_CHECKING, Any

from family_assistant.events.sources import EventSource
from family_assistant.storage.events import EventSourceType

if TYPE_CHECKING:
    from family_assistant.events.processor import EventProcessor

logger = logging.getLogger(__name__)


class IndexingEventType(str, Enum):
    """Types of indexing events."""

    DOCUMENT_READY = "document_ready"
    INDEXING_FAILED = "indexing_failed"


class IndexingSource(EventSource):
    """Event source for document indexing events."""

    def __init__(self) -> None:
        """Initialize indexing event source."""
        self.processor: EventProcessor | None = None
        self._running = False
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._processor_task: asyncio.Task | None = None

    @property
    def source_id(self) -> str:
        """Return the source identifier."""
        return EventSourceType.indexing.value

    async def start(self, processor: "EventProcessor") -> None:
        """Start the indexing event source."""
        self.processor = processor
        self._running = True
        self._processor_task = asyncio.create_task(self._process_events())
        logger.info(f"Started indexing event source [{self.source_id}]")

    async def stop(self) -> None:
        """Stop the indexing event source."""
        self._running = False
        if self._processor_task:
            self._processor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._processor_task
        logger.info(f"Stopped indexing event source [{self.source_id}]")

    async def emit_event(self, event_data: dict[str, Any]) -> None:
        """
        Emit an indexing event.

        Args:
            event_data: Event data containing at minimum:
                - event_type: IndexingEventType value
                - document_id: ID of the document
                - Additional fields based on event type
        """
        if not self._running:
            logger.warning(
                f"Attempted to emit event while source not running: {event_data}"
            )
            return

        try:
            self._event_queue.put_nowait(event_data)
        except asyncio.QueueFull:
            logger.error(
                f"Event queue full, dropping indexing event: {event_data.get('event_type')}"
            )

    async def _process_events(self) -> None:
        """Process events from the queue asynchronously."""
        while self._running:
            try:
                # Wait for events with timeout to allow checking _running flag
                event = await asyncio.wait_for(self._event_queue.get(), timeout=1.0)

                # Send to processor
                if self.processor:
                    await self.processor.process_event(self.source_id, event)

            except asyncio.TimeoutError:
                # No event within timeout, continue loop to check _running
                continue
            except Exception as e:
                logger.error(f"Error processing queued event: {e}", exc_info=True)
