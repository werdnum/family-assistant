"""
Test Home Assistant event source reconnection and health checking.
"""

import asyncio
import contextlib
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.events.home_assistant_source import HomeAssistantSource
from family_assistant.events.processor import EventProcessor
from family_assistant.events.webhook_source import WebhookEventSource
from family_assistant.storage.database import Database


class _SilentWebsocketClient:
    """Stands in for homeassistant_api's WebsocketClient: connects, then fires no events."""

    def __init__(self, api_url: str, token: str) -> None:
        self.api_url = api_url
        self.token = token

    def __enter__(self) -> "_SilentWebsocketClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    @contextlib.contextmanager
    def listen_events(self) -> Iterator[Iterator[object]]:
        yield iter([])


@pytest.mark.asyncio
async def test_exponential_backoff_reconnection() -> None:
    """Test that reconnection uses exponential backoff."""
    # Create mock client
    mock_client = MagicMock()
    mock_client.api_url = "http://localhost:8123/api"
    mock_client.token = "test_token"
    mock_client.verify_ssl = True

    source = HomeAssistantSource(mock_client)

    # Verify initial state
    assert source._reconnect_delay == source._base_reconnect_delay
    assert source._reconnect_attempts == 0

    sleep_calls: list[float] = []
    observed_retries = asyncio.Event()

    async def mock_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        if len(sleep_calls) >= 2:
            observed_retries.set()
        # Yield to the observer without waiting through the production backoff.
        await original_sleep(0)

    original_sleep = asyncio.sleep
    source_asyncio = SimpleNamespace(
        sleep=mock_sleep,
        to_thread=AsyncMock(side_effect=RuntimeError("Connection failed")),
    )
    # Patch only the source's module reference, leaving other session tasks alone.
    with patch("family_assistant.events.home_assistant_source.asyncio", source_asyncio):
        source._running = True
        task = asyncio.create_task(source._websocket_loop())
        try:
            await asyncio.wait_for(observed_retries.wait(), timeout=5)
        finally:
            source._running = False
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        # Verify exponential backoff was applied
        # At least one reconnection attempt should have been made
        assert source._reconnect_attempts >= 2

        # Filter out the test's own sleep calls and only keep reconnect delays
        reconnect_delays = [d for d in sleep_calls if d >= source._base_reconnect_delay]
        assert len(reconnect_delays) >= 2

        # First delay should be base_delay * 2^1 after first attempt
        expected_first_delay = min(
            source._base_reconnect_delay * (2**1), source._max_reconnect_delay
        )
        assert reconnect_delays[0] == expected_first_delay

        # Second delay should follow exponential backoff (2^2)
        expected_second_delay = min(
            source._base_reconnect_delay * (2**2), source._max_reconnect_delay
        )
        assert reconnect_delays[1] == expected_second_delay


@pytest.mark.asyncio
async def test_health_check_triggers_reconnection() -> None:
    """A silent connection whose API probe fails is marked unhealthy and its websocket is torn down."""
    mock_client = MagicMock()
    mock_client.api_url = "http://localhost:8123/api"
    mock_client.token = "test_token"
    mock_client.get_states.side_effect = ConnectionError("Home Assistant unreachable")

    source = HomeAssistantSource(mock_client)
    source._connection_healthy = True
    source._last_event_time = 0
    source._health_check_interval = 0
    websocket_task = asyncio.create_task(asyncio.Event().wait())
    source._websocket_task = websocket_task

    await source._run_health_check()

    assert source._connection_healthy is False
    await asyncio.wait({websocket_task}, timeout=5)
    assert websocket_task.cancelled()


@pytest.mark.asyncio
async def test_successful_reconnection_resets_attempts() -> None:
    """Test that successful connection resets reconnection attempts."""
    # Create mock client
    mock_client = MagicMock()
    mock_client.api_url = "http://localhost:8123/api"
    mock_client.token = "test_token"

    source = HomeAssistantSource(mock_client)

    # Simulate some failed attempts
    source._reconnect_attempts = 5
    source._reconnect_delay = 80.0  # Would be high after 5 attempts

    # Mock successful WebSocket connection
    with patch(
        "family_assistant.events.home_assistant_source.WebsocketClient"
    ) as mock_ws_class:
        mock_ws = MagicMock()
        mock_ws.__enter__ = MagicMock(return_value=mock_ws)
        mock_ws.__exit__ = MagicMock(return_value=None)

        # Mock the listen_events method
        mock_event_listener = MagicMock()
        mock_event_listener.__enter__ = MagicMock(return_value=iter([]))
        mock_event_listener.__exit__ = MagicMock(return_value=None)
        mock_ws.listen_events = MagicMock(return_value=mock_event_listener)

        mock_ws_class.return_value = mock_ws

        # Call connect method which should succeed
        source._connect_and_listen()

        # Verify WebsocketClient was instantiated with correct parameters
        assert mock_ws_class.called

        # Verify connection state was reset
        assert source._connection_healthy
        assert source._reconnect_attempts == 0
        assert source._reconnect_delay == source._base_reconnect_delay


@pytest.mark.asyncio
async def test_event_processor_health_status() -> None:
    """Health status reports a connected Home Assistant source's state, and 'unknown' for untracked sources."""
    mock_client = MagicMock()
    mock_client.api_url = "http://localhost:8123/api"
    mock_client.token = "test_token"
    ha_source = HomeAssistantSource(mock_client)
    with patch(
        "family_assistant.events.home_assistant_source.WebsocketClient",
        _SilentWebsocketClient,
    ):
        ha_source._connect_and_listen()

    processor = EventProcessor(
        {"home_assistant": ha_source, "webhook": WebhookEventSource()},
        timezone=ZoneInfo("Australia/Sydney"),
    )

    status = await processor.get_health_status()

    ha_status = status["sources"]["home_assistant"]
    assert ha_status.get("healthy") is True
    assert ha_status.get("reconnect_attempts") == 0
    assert ha_status.get("last_event_time", 0) > 0
    assert status["sources"]["webhook"] == {"status": "unknown"}


@pytest.mark.asyncio
async def test_event_processor_health_status_counts_enabled_listeners(
    db_engine: AsyncEngine,
) -> None:
    """A started processor's health status counts the enabled listeners it has cached, per source."""
    db = Database(db_engine)
    for name, enabled in (("Front door", True), ("Back door", True), ("Garage", False)):
        await db.events.create_event_listener(
            name=name,
            source_id="home_assistant",
            match_conditions={"entity_id": "binary_sensor.door"},
            conversation_id="test_conversation",
            enabled=enabled,
        )
    processor = EventProcessor(
        sources={},
        get_db_context_func=lambda: Database(db_engine),
        timezone=ZoneInfo("Australia/Sydney"),
    )

    await processor.start()
    try:
        status = await processor.get_health_status()
    finally:
        await processor.stop()

    assert status["processor_running"] is True
    assert status["listener_cache"]["listener_count"] == 2
    assert status["listener_cache"]["by_source"] == {"home_assistant": 2}
