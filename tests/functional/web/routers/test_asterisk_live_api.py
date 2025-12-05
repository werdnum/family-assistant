"""Functional tests for Asterisk Live API."""

import contextlib
import json
import os
import time
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Mock google.genai before importing the app/router to avoid ImportError handling
with patch.dict("sys.modules", {"google.genai": MagicMock()}):
    from family_assistant.web.app_creator import app


@pytest.fixture
def mock_genai() -> AsyncIterator[tuple[MagicMock, AsyncMock]]:
    """Mock the Google GenAI client."""
    with patch(
        "family_assistant.web.routers.asterisk_live_api.genai"
    ) as mock_genai_module:
        # Create the client mock
        client_mock = MagicMock()
        mock_genai_module.Client.return_value = client_mock

        # Create the session mock
        # We use MagicMock for session because receive() is not awaitable (it returns an iterator)
        # but send() is awaitable.
        session_mock = MagicMock()
        session_mock.send = AsyncMock()

        # Mock receive to return an empty iterator or control it
        # We need an async iterator for receive()
        async def async_iter() -> AsyncIterator[None]:
            # Yield nothing effectively waiting forever or finishing immediately
            if False:
                yield None

        session_mock.receive.return_value = async_iter()

        # Mock the context manager for connect
        connect_context = AsyncMock()
        connect_context.__aenter__.return_value = session_mock
        connect_context.__aexit__.return_value = None

        client_mock.aio.live.connect.return_value = connect_context

        # Also ensure types are available
        mock_genai_module.types = MagicMock()

        yield client_mock, session_mock


@pytest.mark.no_db
def test_asterisk_connection_flow(mock_genai: tuple[MagicMock, AsyncMock]) -> None:
    """Test the basic flow of connecting from Asterisk."""
    client_mock, session_mock = mock_genai

    # Set API Key
    with (
        patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}),
        TestClient(app) as client,
        client.websocket_connect("/api/asterisk/live") as websocket,
    ):
        # 1. Send MEDIA_START (Asterisk -> Server)
        # Plain text format
        websocket.send_text(
            "MEDIA_START connection_id:test-conn format:slin16 optimal_frame_size:320"
        )

        # Check that Gemini client was initialized
        assert client_mock.aio.live.connect.called

        # 2. Send Audio (Asterisk -> Server)
        audio_chunk = b"\x00" * 320
        websocket.send_bytes(audio_chunk)

        # Give time for async processing
        time.sleep(0.5)

        # Check that audio was forwarded to Gemini
        assert session_mock.send.called
        call_args = session_mock.send.call_args
        assert call_args is not None
        kwargs = call_args.kwargs
        assert "input" in kwargs
        assert kwargs["input"]["mime_type"] == "audio/pcm;rate=16000"

        # 3. Send HANGUP
        websocket.send_text("HANGUP")

        # WebSocket should close (handled by context manager exit usually, but HANGUP triggers close from server side)
        # We can check if receive raises disconnect
        with contextlib.suppress(Exception):
            websocket.receive_text()


@pytest.mark.no_db
def test_asterisk_json_protocol(mock_genai: tuple[MagicMock, AsyncMock]) -> None:
    """Test using JSON protocol for Asterisk control messages."""
    client_mock, session_mock = mock_genai

    with (
        patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}),
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

        assert client_mock.aio.live.connect.called

        # Send Audio
        websocket.send_bytes(b"\x00" * 320)

        # Give time for async processing
        time.sleep(0.5)

        # Verify 16kHz rate
        assert session_mock.send.called
        call_args = session_mock.send.call_args
        kwargs = call_args.kwargs
        assert kwargs["input"]["mime_type"] == "audio/pcm;rate=16000"
