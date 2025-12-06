"""Functional tests for Asterisk Live API."""

import contextlib
import json
from collections.abc import AsyncIterator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from family_assistant.web.app_creator import app
from family_assistant.web.dependencies import get_live_audio_client
from family_assistant.web.voice_client import LiveAudioClient, LiveAudioSession
from tests.helpers import wait_for_condition


@pytest.fixture
def mock_live_audio_client() -> Generator[tuple[MagicMock, MagicMock]]:
    """Mock the LiveAudioClient."""
    # Create the client mock
    client_mock = MagicMock(spec=LiveAudioClient)

    # Create the session mock
    session_mock = MagicMock(spec=LiveAudioSession)
    session_mock.send = AsyncMock()

    async def async_iter() -> AsyncIterator[None]:
        if False:
            yield None

    session_mock.receive.return_value = async_iter()

    # Mock connect context manager
    connect_context = AsyncMock()
    connect_context.__aenter__.return_value = session_mock
    connect_context.__aexit__.return_value = None

    client_mock.connect.return_value = connect_context

    # Override dependency
    app.dependency_overrides[get_live_audio_client] = lambda: client_mock

    yield client_mock, session_mock

    # Cleanup
    app.dependency_overrides = {}


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_asterisk_connection_flow(
    mock_live_audio_client: tuple[MagicMock, MagicMock],
) -> None:
    """Test the basic flow of connecting from Asterisk."""
    client_mock, session_mock = mock_live_audio_client

    with (
        TestClient(app) as client,
        client.websocket_connect("/api/asterisk/live") as websocket,
    ):
        # 1. Send MEDIA_START (Asterisk -> Server)
        # Plain text format
        websocket.send_text(
            "MEDIA_START connection_id:test-conn format:slin16 optimal_frame_size:320"
        )

        # Allow async loop to process message and connect
        await wait_for_condition(lambda: client_mock.connect.called)

        # Check that Gemini client was initialized
        # This confirms that we processed the config before connecting
        assert client_mock.connect.called

        # 2. Send Audio (Asterisk -> Server)
        audio_chunk = b"\x00" * 320
        websocket.send_bytes(audio_chunk)

        # Give time for async processing
        await wait_for_condition(lambda: session_mock.send.called)

        # Check that audio was forwarded to Gemini
        assert session_mock.send.called

        # 3. Send HANGUP
        websocket.send_text("HANGUP")

        # WebSocket should close (handled by context manager exit usually, but HANGUP triggers close from server side)
        # We can check if receive raises disconnect
        with contextlib.suppress(Exception):
            websocket.receive_text()


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_asterisk_json_protocol(
    mock_live_audio_client: tuple[MagicMock, MagicMock],
) -> None:
    """Test using JSON protocol for Asterisk control messages."""
    client_mock, session_mock = mock_live_audio_client

    with (
        TestClient(app) as client,
        client.websocket_connect("/api/asterisk/live") as websocket,
    ):
        # Send JSON MEDIA_START
        start_event = {
            "event": "MEDIA_START",
            "connection_id": "json-conn",
            "format": "slin16",
            "optimal_frame_size": 320,
        }
        websocket.send_text(json.dumps(start_event))

        await wait_for_condition(lambda: client_mock.connect.called)
        assert client_mock.connect.called

        # Send Audio
        websocket.send_bytes(b"\x00" * 320)

        # Give time for async processing
        await wait_for_condition(lambda: session_mock.send.called)

        # Verify send called
        assert session_mock.send.called
