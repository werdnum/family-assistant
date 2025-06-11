"""
Test Home Assistant event source reconnection and health checking.
"""

import asyncio
import contextlib
from unittest.mock import MagicMock, patch

import pytest

from family_assistant.events.home_assistant_source import HomeAssistantSource
from family_assistant.events.processor import EventProcessor


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

    # Simulate multiple failed connection attempts
    with (
        patch.object(
            source, "_connect_and_listen", side_effect=Exception("Connection failed")
        ),
        patch("asyncio.to_thread", side_effect=lambda func: func()),
        patch("asyncio.sleep") as mock_sleep,
    ):
        # Run the loop for a bit
        source._running = True
        task = asyncio.create_task(source._websocket_loop())

        # Let it fail a couple times
        await asyncio.sleep(0.01)

        # Stop the loop
        source._running = False
        task.cancel()

        with contextlib.suppress(asyncio.CancelledError):
            await task

        # Verify exponential backoff was applied
        # At least one reconnection attempt should have been made
        assert source._reconnect_attempts > 0

        # Sleep should have been called with calculated delays
        sleep_calls = mock_sleep.call_args_list
        if sleep_calls:
            # The delay increases exponentially
            first_delay = sleep_calls[0][0][0]
            assert first_delay >= source._base_reconnect_delay


@pytest.mark.asyncio
async def test_health_check_triggers_reconnection() -> None:
    """Test that health check triggers reconnection when no events received."""
    # Create mock client
    mock_client = MagicMock()
    mock_client.api_url = "http://localhost:8123/api"
    mock_client.token = "test_token"

    source = HomeAssistantSource(mock_client)
    source._connection_healthy = True
    source._last_event_time = 0  # Very old timestamp
    source._health_check_interval = 0.05  # Fast health checks for testing

    # Mock the websocket task
    source._websocket_task = MagicMock()
    source._websocket_task.done.return_value = False
    source._websocket_task.cancel = MagicMock()

    # Mock the connection test to fail
    with patch.object(source, "_test_connection", return_value=False):
        # Start health check
        source._running = True
        health_task = asyncio.create_task(source._health_check_loop())

        # Wait for health check to detect the issue
        await asyncio.sleep(0.2)

        # Stop the loop
        source._running = False
        health_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await health_task

        # Verify connection was marked unhealthy
        assert not source._connection_healthy
        # Verify websocket task was cancelled to trigger reconnection
        source._websocket_task.cancel.assert_called()


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
    with patch("homeassistant_api.WebsocketClient") as mock_ws_class:
        mock_ws = MagicMock()
        mock_ws.__enter__ = MagicMock(return_value=mock_ws)
        mock_ws.__exit__ = MagicMock(return_value=None)
        mock_ws.listen_events = MagicMock()
        mock_ws.listen_events.return_value.__enter__ = MagicMock(return_value=iter([]))
        mock_ws.listen_events.return_value.__exit__ = MagicMock(return_value=None)
        mock_ws_class.return_value = mock_ws

        # Call connect method
        source._connect_and_listen()

        # Verify connection state was reset
        assert source._connection_healthy
        assert source._reconnect_attempts == 0
        assert source._reconnect_delay == source._base_reconnect_delay


@pytest.mark.asyncio
async def test_event_processor_health_status() -> None:
    """Test that event processor can report health status."""
    # Create mock sources
    mock_ha_source = MagicMock()
    mock_ha_source._connection_healthy = True
    mock_ha_source._reconnect_attempts = 0
    mock_ha_source._last_event_time = 12345.0

    sources = {"home_assistant": mock_ha_source}

    # Create processor
    processor = EventProcessor(sources)  # type: ignore[arg-type]
    processor._running = True
    processor._listener_cache = {"home_assistant": [{"id": 1}, {"id": 2}]}

    # Get health status
    status = await processor.get_health_status()

    # Verify status structure
    assert status["processor_running"] is True
    assert "sources" in status
    assert "home_assistant" in status["sources"]
    assert status["sources"]["home_assistant"]["healthy"] is True
    assert status["sources"]["home_assistant"]["reconnect_attempts"] == 0
    assert status["listener_cache"]["listener_count"] == 2
