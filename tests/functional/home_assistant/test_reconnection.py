"""
Test Home Assistant event source reconnection and health checking.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import janus
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.events.processor import EventProcessor
from family_assistant.events.webhook_source import WebhookEventSource
from family_assistant.plugins.home_assistant.events import HomeAssistantSource
from family_assistant.storage.database import Database
from tests.mocks.home_assistant_client import (
    HA_TEST_TOKEN,
    make_home_assistant_client,
)


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
    source = HomeAssistantSource(make_home_assistant_client().wrapper)

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

    async def failing_to_thread(
        func: Callable[..., object], *args: object, **kwargs: object
    ) -> object:
        raise RuntimeError("Connection failed")

    original_sleep = asyncio.sleep
    source_asyncio = SimpleNamespace(sleep=mock_sleep, to_thread=failing_to_thread)
    # Patch only the source's module reference, leaving other session tasks alone.
    with patch(
        "family_assistant.plugins.home_assistant.events.asyncio", source_asyncio
    ):
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
    home_assistant = make_home_assistant_client()
    home_assistant.api.async_get_states.side_effect = ConnectionError(
        "Home Assistant unreachable"
    )

    source = HomeAssistantSource(home_assistant.wrapper)
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
    source = HomeAssistantSource(
        make_home_assistant_client("http://localhost:8123").wrapper
    )

    # Simulate some failed attempts
    source._reconnect_attempts = 5
    source._reconnect_delay = 80.0  # Would be high after 5 attempts

    connected: list[_SilentWebsocketClient] = []

    def connect(api_url: str, token: str) -> _SilentWebsocketClient:
        client = _SilentWebsocketClient(api_url, token)
        connected.append(client)
        return client

    with patch(
        "family_assistant.plugins.home_assistant.events.WebsocketClient", connect
    ):
        source._connect_and_listen()

        assert [(c.api_url, c.token) for c in connected] == [
            ("ws://localhost:8123/api/websocket", HA_TEST_TOKEN)
        ]

        # Verify connection state was reset
        assert source._connection_healthy
        assert source._reconnect_attempts == 0
        assert source._reconnect_delay == source._base_reconnect_delay


@pytest.mark.asyncio
async def test_event_processor_health_status() -> None:
    """Health status reports a connected Home Assistant source's state, and 'unknown' for untracked sources."""
    ha_source = HomeAssistantSource(make_home_assistant_client().wrapper)
    with patch(
        "family_assistant.plugins.home_assistant.events.WebsocketClient",
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


@pytest.mark.asyncio
async def test_event_processor_exits_quietly_when_queue_shuts_down(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Shutting the queue down in stop() is teardown, not a processing error."""
    source = HomeAssistantSource(make_home_assistant_client().wrapper)
    source._running = True
    source._event_queue = janus.Queue()
    source._event_queue.shutdown()

    with caplog.at_level(logging.ERROR):
        await asyncio.wait_for(source._process_events(), timeout=5)

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    await source._event_queue.wait_closed()
