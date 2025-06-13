"""
Home Assistant event source implementation.
"""

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

import homeassistant_api as ha_api
import janus
from homeassistant_api import WebsocketClient

from family_assistant.events.sources import EventSource
from family_assistant.storage.events import EventSourceType

if TYPE_CHECKING:
    from family_assistant.events.processor import EventProcessor

logger = logging.getLogger(__name__)


class HomeAssistantSource(EventSource):
    """Event source for Home Assistant state changes."""

    def __init__(
        self, client: ha_api.Client, event_types: list[str] | None = None
    ) -> None:
        """
        Initialize Home Assistant event source.

        Args:
            client: Shared Home Assistant API client
            event_types: List of event types to subscribe to (default: all)
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
        # Event types to subscribe to
        self.event_types = event_types

        # Reconnection parameters with exponential backoff
        self._base_reconnect_delay = 5.0  # Base delay in seconds
        self._max_reconnect_delay = 300.0  # Max delay (5 minutes)
        self._reconnect_delay = self._base_reconnect_delay
        self._reconnect_attempts = 0

        # Health check parameters
        self._health_check_interval = 30.0  # Check every 30 seconds
        self._health_check_task: asyncio.Task | None = None
        self._last_event_time = 0.0
        self._connection_healthy = False

        # Janus queue for thread-to-asyncio communication
        self._event_queue: janus.Queue[dict[str, Any]] | None = None
        self._processor_task: asyncio.Task | None = None

    @property
    def source_id(self) -> str:
        """Return the source identifier."""
        return EventSourceType.home_assistant.value

    async def start(self, processor: "EventProcessor") -> None:
        """Start listening for Home Assistant events."""
        self.processor = processor
        self._running = True

        # Initialize janus queue
        self._event_queue = janus.Queue(maxsize=1000)

        self._websocket_task = asyncio.create_task(self._websocket_loop())
        self._processor_task = asyncio.create_task(self._process_events())
        self._health_check_task = asyncio.create_task(self._health_check_loop())
        logger.info(f"Started Home Assistant event source [{self.source_id}]")

    async def stop(self) -> None:
        """Stop listening for events."""
        self._running = False
        tasks = [self._websocket_task, self._processor_task, self._health_check_task]
        for task in tasks:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        # Close janus queue properly
        if self._event_queue:
            logger.debug(f"[{self.source_id}] Closing janus queue")
            try:
                await self._event_queue.aclose()
            except Exception as e:
                logger.error(
                    f"[{self.source_id}] Error closing janus queue: {e}", exc_info=True
                )
            self._event_queue = None

        logger.info(f"Stopped Home Assistant event source [{self.source_id}]")

    async def _websocket_loop(self) -> None:
        """Main WebSocket connection loop with exponential backoff reconnection."""
        while self._running:
            try:
                # Reset connection state on new attempt
                self._connection_healthy = False

                # Run blocking WebSocket in thread
                await asyncio.to_thread(self._connect_and_listen)

                # If we get here, connection was closed normally
                logger.warning(
                    f"[{self.source_id}] Home Assistant WebSocket connection closed"
                )
                self._reconnect_attempts += 1

            except Exception as e:
                logger.error(
                    f"[{self.source_id}] Home Assistant WebSocket error: {e}",
                    exc_info=True,
                )
                self._reconnect_attempts += 1

            if self._running:
                # Calculate exponential backoff
                self._reconnect_delay = min(
                    self._base_reconnect_delay * (2**self._reconnect_attempts),
                    self._max_reconnect_delay,
                )

                logger.info(
                    f"Reconnecting to Home Assistant in {self._reconnect_delay} seconds "
                    f"(attempt {self._reconnect_attempts})"
                )
                await asyncio.sleep(self._reconnect_delay)

    def _connect_and_listen(self) -> None:
        """Connect to Home Assistant WebSocket and listen for events (blocking)."""
        logger.info(f"[{self.source_id}] Connecting to Home Assistant WebSocket")

        try:
            # Convert HTTP URL to WebSocket URL
            ws_url = self.api_url.replace("http://", "ws://").replace(
                "https://", "wss://"
            )
            ws_url = ws_url.rstrip("/api") + "/api/websocket"

            logger.info(f"[{self.source_id}] Connecting to WebSocket at {ws_url}")

            # Create WebSocket client and listen for state_changed events
            with WebsocketClient(
                api_url=ws_url,
                token=self.token,
                # Note: WebsocketClient doesn't have a verify_ssl parameter
            ) as ws_client:
                logger.info(f"[{self.source_id}] Connected to Home Assistant WebSocket")

                # Mark connection as healthy and reset reconnect attempts
                self._connection_healthy = True
                self._reconnect_attempts = 0
                self._reconnect_delay = self._base_reconnect_delay
                self._last_event_time = time.time()

                # Listen for configured event types
                # We need to handle multiple event types
                for event_type in self.event_types or ["all"]:
                    logger.info(
                        f"[{self.source_id}] Subscribing to {event_type} events"
                    )

                # Listen to all events and filter by type
                with ws_client.listen_events() as events:
                    for event in events:
                        if not self._running:
                            break

                        # Check if this event type is one we're interested in
                        event_type = getattr(event, "event_type", None)
                        if event_type and (
                            self.event_types is None
                            or (self.event_types and event_type in self.event_types)
                        ):
                            # Process the event
                            self._handle_event_sync(event_type, event)

        except Exception as e:
            logger.error(f"WebSocket connection error: {e}")
            raise

    def _handle_event_sync(self, event_type: str, event: Any) -> None:
        """Handle an event synchronously from the thread."""
        try:
            # FiredEvent object may have attributes instead of dict access
            # Try to access as attributes first, fall back to dict access
            event_data = event.data if hasattr(event, "data") else event.get("data", {})

            # Convert event_data to dict if it's not already
            if not isinstance(event_data, dict):
                # Try to extract attributes from object
                event_dict = {}
                for attr in dir(event_data):
                    if not attr.startswith("_"):
                        try:
                            value = getattr(event_data, attr)
                            # Include all values - we'll handle complex objects later
                            event_dict[attr] = value
                        except Exception:
                            pass
                event_data = event_dict

            # Process based on event type
            processed_event: dict[str, Any] = {"event_type": event_type}

            if event_type == "state_changed":
                # Handle state_changed events specially
                entity_id = event_data.get("entity_id")
                if not entity_id:
                    return

                old_state = event_data.get("old_state", {})
                new_state = event_data.get("new_state", {})

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

                processed_event["entity_id"] = entity_id
                processed_event["old_state"] = extract_state_info(old_state)
                processed_event["new_state"] = extract_state_info(new_state)
            else:
                # For other event types, include all event data
                processed_event.update(event_data)

            # Add to queue for async processing
            # Use janus sync queue for thread-safe operations
            if self._event_queue:
                try:
                    self._event_queue.sync_q.put_nowait(processed_event)
                    # Update last event time for health check
                    self._last_event_time = time.time()
                except Exception:  # janus uses standard queue.Full exception
                    logger.warning(f"Event queue full, dropping event: {event_type}")
            else:
                logger.error("Event queue not initialized, dropping event")

        except Exception as e:
            logger.error(f"Error processing {event_type} event: {e}", exc_info=True)

    async def _process_events(self) -> None:
        """Process events from the queue asynchronously."""
        logger.debug("Starting event processor task")

        while self._running:
            if not self._event_queue:
                await asyncio.sleep(0.1)  # Wait for queue initialization
                continue

            try:
                # Use janus async queue for asyncio side
                try:
                    event = await asyncio.wait_for(
                        self._event_queue.async_q.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    # No event within timeout, continue loop to check _running
                    continue

                # Send to processor
                if self.processor:
                    await self.processor.process_event(self.source_id, event)
                else:
                    logger.error("Event processor is not set - event will be dropped")

                # Mark task as done for proper queue cleanup
                self._event_queue.async_q.task_done()

            except Exception as e:
                logger.error(f"Error processing queued event: {e}", exc_info=True)

    async def _health_check_loop(self) -> None:
        """Periodically check connection health."""
        await asyncio.sleep(10)  # Initial delay before starting health checks

        while self._running:
            try:
                # Check if connection is marked as healthy
                if self._connection_healthy:
                    # Check if we've received any events recently
                    time_since_last_event = time.time() - self._last_event_time

                    # If no events for extended period, test the connection
                    if time_since_last_event > 300:  # 5 minutes
                        logger.warning(
                            f"No events received for {time_since_last_event:.0f} seconds, "
                            "checking Home Assistant connection"
                        )

                        # Try to verify connection with a simple API call
                        connection_ok = await self._test_connection()

                        if not connection_ok:
                            logger.error("Home Assistant connection test failed")
                            # Force reconnection by marking unhealthy
                            self._connection_healthy = False
                            # Cancel websocket task to trigger reconnection
                            if self._websocket_task and not self._websocket_task.done():
                                self._websocket_task.cancel()
                else:
                    # Connection is not healthy, log status
                    logger.debug(
                        f"Home Assistant connection unhealthy, reconnect attempt "
                        f"{self._reconnect_attempts} pending"
                    )

                # Wait before next health check
                await asyncio.sleep(self._health_check_interval)

            except asyncio.CancelledError:
                # Task is being cancelled, exit cleanly
                break
            except Exception as e:
                logger.error(f"Error in health check loop: {e}", exc_info=True)
                await asyncio.sleep(self._health_check_interval)

    async def _test_connection(self) -> bool:
        """Test if Home Assistant connection is working."""
        try:
            # Use the regular client to test API connectivity
            # This is a lightweight call that should work if HA is accessible
            states = await asyncio.to_thread(self.client.get_states)
            return len(states) > 0
        except Exception as e:
            logger.error(f"Connection test failed: {e}")
            return False
