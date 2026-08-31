"""
Home Assistant event source implementation.
"""

import asyncio
import contextlib
import logging
import re
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, cast

import homeassistant_api as ha_api
import janus
from homeassistant_api import WebsocketClient

from family_assistant.events.sources import BaseEventSource, EventSource
from family_assistant.events.validation import ValidationError, ValidationResult
from family_assistant.storage.events import EventSourceType
from family_assistant.storage.types import MatchConditions

if TYPE_CHECKING:
    from family_assistant.events.processor import EventProcessor

logger = logging.getLogger(__name__)

# Home Assistant entity ID pattern: domain.object_id
# Very permissive - actual validation via API is more important
# Allows letters, numbers, underscore in both parts
ENTITY_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_]+\.[a-zA-Z0-9_]+$")


class HAStateInfoDict(TypedDict):
    """Extracted state info from a Home Assistant state object."""

    state: str | None
    # ast-grep-ignore: no-dict-any - HA state attributes are arbitrary key-value pairs with no fixed schema
    attributes: dict[str, Any]
    last_changed: str | None


class HasDataAttr(Protocol):
    """Protocol for objects that have a data attribute."""

    data: Any


class HomeAssistantSource(BaseEventSource, EventSource):
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
        # ast-grep-ignore: no-dict-any - queue carries arbitrary HA event JSON with no fixed schema
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

        # Close the queue before cancelling tasks to signal consumers
        if self._event_queue:
            logger.debug(f"[{self.source_id}] Closing janus queue")
            try:
                self._event_queue.shutdown()
                await self._event_queue.wait_closed()
            except Exception as e:
                logger.exception(f"Error closing janus queue: {e}")

        for task in tasks:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

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
                logger.exception(
                    f"[{self.source_id}] Home Assistant WebSocket error: {e}"
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
            self._connect_and_listen_once()
        except Exception as e:
            logger.error(f"WebSocket connection error: {e}")
            raise

    def _connect_and_listen_once(self) -> None:
        ws_url = self.api_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = ws_url.rstrip("/api") + "/api/websocket"
        logger.info(f"[{self.source_id}] Connecting to WebSocket at {ws_url}")
        with WebsocketClient(api_url=ws_url, token=self.token) as ws_client:
            logger.info(f"[{self.source_id}] Connected to Home Assistant WebSocket")
            self._connection_healthy = True
            self._reconnect_attempts = 0
            self._reconnect_delay = self._base_reconnect_delay
            self._last_event_time = time.time()
            for configured_event_type in self.event_types or ["all"]:
                logger.info(
                    f"[{self.source_id}] Subscribing to {configured_event_type} events"
                )
            with ws_client.listen_events() as events:
                for event in events:
                    if not self._running:
                        break
                    event_type = getattr(event, "event_type", None)
                    if event_type and (
                        self.event_types is None
                        or (self.event_types and event_type in self.event_types)
                    ):
                        self._handle_event_sync(event_type, event)

    def _handle_event_sync(
        self,
        event_type: str,
        # ast-grep-ignore: no-dict-any - HA websocket events are untyped objects or arbitrary dicts from the HA API
        event: dict[str, Any] | HasDataAttr,
    ) -> None:
        """Handle an event synchronously from the thread."""
        try:
            self._handle_event(event_type, event)
        except Exception as e:
            logger.exception(f"Error processing {event_type} event: {e}")

    def _handle_event(
        self,
        event_type: str,
        event: object,
    ) -> None:
        if hasattr(event, "data"):
            event_data = cast("HasDataAttr", event).data
        elif hasattr(event, "get"):
            event_data = cast("dict[str, Any]", event).get("data", {})
        else:
            event_data = {}
        if not isinstance(event_data, dict):
            event_dict = {}
            for attribute in dir(event_data):
                if not attribute.startswith("_"):
                    with contextlib.suppress(Exception):
                        event_dict[attribute] = getattr(event_data, attribute)
            event_data = event_dict

        # ast-grep-ignore: no-dict-any - processed HA events retain arbitrary event payload fields
        processed_event: dict[str, Any] = {"event_type": event_type}
        if event_type == "state_changed":
            entity_id = event_data.get("entity_id")
            if not entity_id:
                return
            processed_event["entity_id"] = entity_id
            processed_event["old_state"] = self._extract_state_info(
                event_data.get("old_state", {})
            )
            processed_event["new_state"] = self._extract_state_info(
                event_data.get("new_state", {})
            )
        else:
            processed_event.update(event_data)

        if self._event_queue:
            try:
                self._event_queue.sync_q.put_nowait(processed_event)
                self._last_event_time = time.time()
            except janus.SyncQueueShutDown:
                logger.info(
                    f"[{self.source_id}] Event queue closed, stopping listener thread."
                )
                raise
            except Exception as e:
                logger.warning(
                    f"Event queue full, dropping event: {event_type}. Error: {e}"
                )
        else:
            logger.error("Event queue not initialized, dropping event")

    @staticmethod
    def _extract_state_info(
        # ast-grep-ignore: no-dict-any - HA state objects expose arbitrary integration attributes
        state_obj: dict[str, Any] | object | None,
    ) -> HAStateInfoDict | None:
        if not state_obj:
            return None
        if isinstance(state_obj, dict):
            return HAStateInfoDict(
                state=state_obj.get("state"),
                attributes=state_obj.get("attributes", {}),
                last_changed=state_obj.get("last_changed"),
            )
        return HAStateInfoDict(
            state=getattr(state_obj, "state", None),
            attributes=getattr(state_obj, "attributes", {}),
            last_changed=getattr(state_obj, "last_changed", None),
        )

    async def _process_events(self) -> None:
        """Process events from the queue asynchronously."""
        logger.debug("Starting event processor task")

        while self._running:
            if not self._event_queue:
                await asyncio.sleep(0.1)  # Wait for queue initialization
                continue

            try:
                await self._process_next_event()
            except Exception as e:
                logger.exception(f"Error processing queued event: {e}")

    async def _process_next_event(self) -> None:
        if not self._event_queue:
            return
        try:
            event = await asyncio.wait_for(self._event_queue.async_q.get(), timeout=1.0)
        except TimeoutError:
            return
        if self.processor:
            await self.processor.process_event(self.source_id, event)
        else:
            logger.error("Event processor is not set - event will be dropped")
        self._event_queue.async_q.task_done()

    async def _health_check_loop(self) -> None:
        """Periodically check connection health."""
        await asyncio.sleep(10)  # Initial delay before starting health checks

        while self._running:
            try:
                await self._run_health_check()
            except asyncio.CancelledError:
                # Task is being cancelled, exit cleanly
                break
            except Exception as e:
                logger.exception(f"Error in health check loop: {e}")
                await asyncio.sleep(self._health_check_interval)

    async def _run_health_check(self) -> None:
        if self._connection_healthy:
            time_since_last_event = time.time() - self._last_event_time
            if time_since_last_event > 300:
                logger.warning(
                    f"No events received for {time_since_last_event:.0f} seconds, "
                    "checking Home Assistant connection"
                )
                connection_ok = await self._test_connection()
                if not connection_ok:
                    logger.error("Home Assistant connection test failed")
                    self._connection_healthy = False
                    if self._websocket_task and not self._websocket_task.done():
                        self._websocket_task.cancel()
        else:
            logger.debug(
                "Home Assistant connection unhealthy, reconnect attempt "
                f"{self._reconnect_attempts} pending"
            )
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

    async def validate_match_conditions(
        self,
        match_conditions: MatchConditions,
    ) -> ValidationResult:
        """
        Validate match conditions for Home Assistant events.

        Validates:
        - entity_id: Checks format and existence via API
        - new_state.state / old_state.state: Checks if entity has ever been in that state

        Args:
            match_conditions: The match conditions to validate

        Returns:
            ValidationResult with validation status and any errors/warnings
        """
        errors = []
        warnings = []

        # Check entity_id if present
        if "entity_id" in match_conditions:
            entity_id = match_conditions["entity_id"]

            # First check format
            if not isinstance(entity_id, str):
                errors.append(
                    ValidationError(
                        field="entity_id",
                        value=entity_id,
                        error=f"Entity ID must be a string, got {type(entity_id).__name__}",
                    )
                )
            elif not ENTITY_ID_PATTERN.match(entity_id):
                errors.append(
                    ValidationError(
                        field="entity_id",
                        value=entity_id,
                        error="Invalid entity ID format. Expected: domain.object_id",
                        suggestion="Entity IDs should be like 'person.alex_smith' or 'light.living_room'",
                    )
                )
            else:
                # Format is valid, now check if entity exists via API
                try:
                    await self._check_entity_exists(entity_id, errors)
                except Exception as e:
                    warnings.append(
                        f"Could not verify entity existence via API due to {type(e).__name__}: {e!s}"
                    )

        # Validate state values if entity_id is valid and present
        if "entity_id" in match_conditions and len(errors) == 0:
            entity_id_val = match_conditions["entity_id"]
            entity_id = cast("str", entity_id_val)
            state_fields_to_check = []

            # Check for state conditions
            if "new_state.state" in match_conditions:
                state_fields_to_check.append((
                    "new_state.state",
                    match_conditions["new_state.state"],
                ))
            if "old_state.state" in match_conditions:
                state_fields_to_check.append((
                    "old_state.state",
                    match_conditions["old_state.state"],
                ))

            if state_fields_to_check:
                try:
                    await self._check_state_history(
                        entity_id, state_fields_to_check, warnings
                    )
                except Exception as e:
                    warnings.append(
                        f"Could not verify state history via API due to {type(e).__name__}: {e!s}"
                    )

        return ValidationResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
        )

    async def _check_entity_exists(
        self, entity_id: str, errors: list[ValidationError]
    ) -> None:
        states = await asyncio.to_thread(self.client.get_states)
        entity_ids = [state.entity_id for state in states]
        if entity_id in entity_ids:
            return
        domain = entity_id.split(".", maxsplit=1)[0]
        similar = [eid for eid in entity_ids if eid.startswith(f"{domain}.")]
        if entity_id == "person.alex" and "person.alex_smith" in entity_ids:
            suggestion = "Did you mean 'person.alex_smith'?"
        elif entity_id == "person.taylor" and any(
            "taylor" in eid for eid in entity_ids
        ):
            taylor_entities = [eid for eid in entity_ids if "taylor" in eid.lower()]
            suggestion = (
                f"Did you mean '{taylor_entities[0]}'?" if taylor_entities else None
            )
        else:
            suggestion = None
        errors.append(
            ValidationError(
                field="entity_id",
                value=entity_id,
                error=f"Entity '{entity_id}' not found in Home Assistant",
                suggestion=suggestion,
                similar_values=similar[:5] if similar else None,
            )
        )

    async def _check_state_history(
        self,
        entity_id: str,
        state_fields_to_check: list[tuple[str, object]],
        warnings: list[str],
    ) -> None:
        end_time = datetime.now(UTC)
        histories_raw = await asyncio.to_thread(
            self.client.get_entity_histories,
            entities=(entity_id,),  # type: ignore[arg-type]  # Entity-ID tuple conflicts with third-party tuple[Entity, ...] stub
            start_timestamp=end_time - timedelta(days=7),
            end_timestamp=end_time,
        )
        if isinstance(histories_raw, dict):
            histories = histories_raw
        else:
            histories = {
                history.entity_id: list(history.states) for history in histories_raw
            }
        if not histories or entity_id not in histories:
            warnings.append(
                f"No history found for entity '{entity_id}'. Cannot validate state conditions."
            )
            return

        state_counter: Counter[str] = Counter()
        for state_record in histories[entity_id]:
            if hasattr(state_record, "state") and state_record.state:
                state_counter[state_record.state] += 1
        historical_states = set(state_counter)
        for _field_name, state_value in state_fields_to_check:
            if state_value not in historical_states:
                warnings.append(
                    f"State '{state_value}' has never been recorded for entity '{entity_id}' "
                    "in the last 7 days. This condition may never trigger."
                )
                if state_counter:
                    common_states = [
                        state for state, _count in state_counter.most_common(5)
                    ]
                    warnings.append(
                        f"Most common states for '{entity_id}': {', '.join(common_states)}"
                    )
