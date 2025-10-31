"""
Indexing event source implementation.
"""

import asyncio
import contextlib
import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from family_assistant.events.sources import BaseEventSource, EventSource
from family_assistant.storage.events import EventSourceType

if TYPE_CHECKING:
    from family_assistant.events.processor import EventProcessor

logger = logging.getLogger(__name__)


class IndexingEventType(StrEnum):
    """Types of indexing events."""

    DOCUMENT_READY = "document_ready"
    INDEXING_FAILED = "indexing_failed"


class IndexingSource(BaseEventSource, EventSource):
    """Event source for document indexing events."""

    def __init__(self) -> None:
        """Initialize indexing event source."""
        self.processor: EventProcessor | None = None
        self._running = False
        self._event_queue: asyncio.Queue[
            # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
            tuple[dict[str, Any], asyncio.Future[None]]
        ] = asyncio.Queue(maxsize=1000)
        self._processor_task: asyncio.Task | None = None
        self._pending_events: set[asyncio.Future[None]] = set()

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

    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    async def emit_event(self, event_data: dict[str, Any]) -> asyncio.Future[None]:
        """
        Emit an indexing event.

        Args:
            event_data: Event data containing at minimum:
                - event_type: IndexingEventType value
                - document_id: ID of the document
                - Additional fields based on event type

        Returns:
            A Future that completes when the event has been processed
        """
        future = asyncio.get_running_loop().create_future()

        if not self._running:
            logger.warning(
                f"Attempted to emit event while source not running: {event_data}"
            )
            future.set_result(None)
            return future

        try:
            self._event_queue.put_nowait((event_data, future))
            self._pending_events.add(future)
        except asyncio.QueueFull:
            logger.error(
                f"Event queue full, dropping indexing event: {event_data.get('event_type')}"
            )
            future.set_exception(
                asyncio.QueueFull(
                    f"Event queue full, dropping event: {event_data.get('event_type')}"
                )
            )

        return future

    async def _process_events(self) -> None:
        """Process events from the queue asynchronously."""
        while self._running:
            try:
                # Wait for events with timeout to allow checking _running flag
                event_data, future = await asyncio.wait_for(
                    self._event_queue.get(), timeout=1.0
                )

                # Send to processor
                try:
                    if self.processor:
                        await self.processor.process_event(self.source_id, event_data)
                    future.set_result(None)
                except Exception as e:
                    future.set_exception(e)
                    raise
                finally:
                    # Clean up the future from pending set
                    self._pending_events.discard(future)

            except TimeoutError:
                # No event within timeout, continue loop to check _running
                continue
            except Exception as e:
                logger.error(f"Error processing queued event: {e}", exc_info=True)

    async def wait_for_pending_events(self, timeout: float = 10.0) -> None:
        """Wait for all pending events to be processed.

        Args:
            timeout: Maximum time to wait in seconds

        Raises:
            asyncio.TimeoutError: If timeout is reached before all events are processed
        """
        start_time = asyncio.get_running_loop().time()
        while self._event_queue.qsize() > 0 or self._pending_events:
            if asyncio.get_running_loop().time() - start_time > timeout:
                raise TimeoutError(
                    f"Timeout waiting for events. Queue size: {self._event_queue.qsize()}, "
                    f"Pending futures: {len(self._pending_events)}"
                )
            await asyncio.sleep(0.01)
