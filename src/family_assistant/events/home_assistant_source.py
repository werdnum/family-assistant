"""
Home Assistant event source implementation.
"""

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

import homeassistant_api as ha_api
from homeassistant_api import WebsocketClient

from family_assistant.events.sources import EventSource
from family_assistant.storage.events import EventSourceType

if TYPE_CHECKING:
    from family_assistant.events.processor import EventProcessor

logger = logging.getLogger(__name__)


class HomeAssistantSource(EventSource):
    """Event source for Home Assistant state changes."""

    def __init__(self, client: ha_api.Client) -> None:
        """
        Initialize Home Assistant event source.

        Args:
            client: Shared Home Assistant API client
        """
        self.client = client
        # Extract connection info from client to create WebSocket client
        # The API URL needs to be converted to WebSocket URL
        self.api_url = getattr(client, "api_url", "")
        self.token = getattr(client, "token", "")
        self.verify_ssl = getattr(client, "verify_ssl", True)
        self.processor: EventProcessor | None = None
        self._websocket_task: asyncio.Task | None = None
        self._running = False
        self._reconnect_delay = 5.0  # seconds
        # Queue for thread-safe event passing (limit to prevent memory issues)
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._processor_task: asyncio.Task | None = None

    @property
    def source_id(self) -> str:
        """Return the source identifier."""
        return EventSourceType.home_assistant.value

    async def start(self, processor: "EventProcessor") -> None:
        """Start listening for Home Assistant events."""
        self.processor = processor
        self._running = True
        self._websocket_task = asyncio.create_task(self._websocket_loop())
        self._processor_task = asyncio.create_task(self._process_events())
        logger.info("Started Home Assistant event source")

    async def stop(self) -> None:
        """Stop listening for events."""
        self._running = False
        if self._websocket_task:
            self._websocket_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._websocket_task
        if self._processor_task:
            self._processor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._processor_task
        logger.info("Stopped Home Assistant event source")

    async def _websocket_loop(self) -> None:
        """Main WebSocket connection loop with reconnection."""
        while self._running:
            try:
                # Run blocking WebSocket in thread
                await asyncio.to_thread(self._connect_and_listen)
            except Exception as e:
                logger.error(f"Home Assistant WebSocket error: {e}", exc_info=True)
                if self._running:
                    logger.info(
                        f"Reconnecting to Home Assistant in {self._reconnect_delay} seconds"
                    )
                    await asyncio.sleep(self._reconnect_delay)

    def _connect_and_listen(self) -> None:
        """Connect to Home Assistant WebSocket and listen for events (blocking)."""
        logger.info("Connecting to Home Assistant WebSocket")

        try:
            # Convert HTTP URL to WebSocket URL
            ws_url = self.api_url.replace("http://", "ws://").replace(
                "https://", "wss://"
            )
            ws_url = ws_url.rstrip("/api") + "/api/websocket"

            logger.info(f"Connecting to WebSocket at {ws_url}")

            # Create WebSocket client and listen for state_changed events
            with WebsocketClient(
                api_url=ws_url,
                token=self.token,
                # Note: WebsocketClient doesn't have a verify_ssl parameter
            ) as ws_client:
                logger.info("Connected to Home Assistant WebSocket")

                # Listen specifically for state_changed events
                with ws_client.listen_events("state_changed") as events:
                    for event in events:
                        if not self._running:
                            break

                        # Process the state change event
                        self._handle_state_change_sync(event)

        except Exception as e:
            logger.error(f"WebSocket connection error: {e}")
            raise

    def _handle_state_change_sync(self, event: Any) -> None:
        """Handle a state change event synchronously from the thread."""
        try:
            # FiredEvent object may have attributes instead of dict access
            # Try to access as attributes first, fall back to dict access
            event_data = event.data if hasattr(event, "data") else event.get("data", {})

            if isinstance(event_data, dict):
                entity_id = event_data.get("entity_id")
            else:
                entity_id = getattr(event_data, "entity_id", None)

            if not entity_id:
                return

            # Extract state data
            if isinstance(event_data, dict):
                old_state = event_data.get("old_state", {})
                new_state = event_data.get("new_state", {})
            else:
                old_state = getattr(event_data, "old_state", {})
                new_state = getattr(event_data, "new_state", {})

            # Helper function to extract state info from dict or object
            def extract_state_info(state_obj: Any) -> dict[str, Any] | None:
                if not state_obj:
                    return None

                if isinstance(state_obj, dict):
                    return {
                        "state": state_obj.get("state"),
                        "attributes": state_obj.get("attributes", {}),
                        "last_changed": state_obj.get("last_changed"),
                    }
                else:
                    # Handle as object with attributes
                    return {
                        "state": getattr(state_obj, "state", None),
                        "attributes": getattr(state_obj, "attributes", {}),
                        "last_changed": getattr(state_obj, "last_changed", None),
                    }

            # Create simplified event data for processing
            processed_event = {
                "entity_id": entity_id,
                "old_state": extract_state_info(old_state),
                "new_state": extract_state_info(new_state),
            }

            # Add to queue for async processing
            # Use thread-safe put_nowait since we're in a thread
            try:
                self._event_queue.put_nowait(processed_event)
            except asyncio.QueueFull:
                logger.warning(f"Event queue full, dropping event for {entity_id}")

        except Exception as e:
            logger.error(f"Error processing state change event: {e}", exc_info=True)

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
