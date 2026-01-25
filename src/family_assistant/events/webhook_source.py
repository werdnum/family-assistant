"""
Webhook event source for processing incoming webhook events.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from family_assistant.events.sources import BaseEventSource, EventSource

if TYPE_CHECKING:
    from family_assistant.events.processor import EventProcessor

logger = logging.getLogger(__name__)


class WebhookEventSource(BaseEventSource, EventSource):
    """
    Event source for incoming webhooks.

    This source receives events from the webhook endpoint and forwards them
    to the event processor for matching against listeners.
    """

    def __init__(self) -> None:
        """Initialize the webhook event source."""
        self.processor: EventProcessor | None = None
        self._running = False

    @property
    def source_id(self) -> str:
        """Return the unique identifier for this source."""
        return "webhook"

    async def start(self, processor: EventProcessor) -> None:
        """Start the webhook source and register the processor."""
        self.processor = processor
        self._running = True
        logger.info("WebhookEventSource started")

    async def stop(self) -> None:
        """Stop the webhook source."""
        self._running = False
        self.processor = None
        logger.info("WebhookEventSource stopped")

    async def emit_event(
        self,
        # ast-grep-ignore: no-dict-any - Webhook payloads have arbitrary structure
        event_data: dict[str, Any],
    ) -> str | None:
        """
        Emit an event to the event processor.

        Called by the webhook router when an event is received.

        Args:
            event_data: The event data from the webhook payload

        Returns:
            Event ID if the event was processed, None if source not running
        """
        if not self._running or not self.processor:
            logger.warning("WebhookEventSource not running, cannot emit event")
            return None

        await self.processor.process_event(self.source_id, event_data)
        logger.debug(
            f"Emitted webhook event: {event_data.get('event_type', 'unknown')}"
        )
        return event_data.get("event_id")
