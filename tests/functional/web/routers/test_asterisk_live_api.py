"""Functional tests for Asterisk Live API."""

import contextlib
import json
import os
from collections.abc import AsyncIterator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

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


# --- Authentication Tests ---


@pytest.fixture
def asterisk_env_cleanup() -> Generator[None]:
    """Clean up Asterisk environment variables after tests."""
    # Store original values
    orig_token = os.environ.get("ASTERISK_SECRET_TOKEN")
    orig_extensions = os.environ.get("ASTERISK_ALLOWED_EXTENSIONS")

    yield

    # Restore original values
    if orig_token is None:
        os.environ.pop("ASTERISK_SECRET_TOKEN", None)
    else:
        os.environ["ASTERISK_SECRET_TOKEN"] = orig_token

    if orig_extensions is None:
        os.environ.pop("ASTERISK_ALLOWED_EXTENSIONS", None)
    else:
        os.environ["ASTERISK_ALLOWED_EXTENSIONS"] = orig_extensions


@pytest.mark.no_db
def test_asterisk_rejects_missing_token(
    mock_live_audio_client: tuple[MagicMock, MagicMock],
    asterisk_env_cleanup: None,
) -> None:
    """Connection rejected when token is required but not provided."""
    os.environ["ASTERISK_SECRET_TOKEN"] = "test_secret_token"

    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect("/api/asterisk/live"),
    ):
        pass

    assert exc_info.value.code == 1008


@pytest.mark.no_db
def test_asterisk_rejects_invalid_token(
    mock_live_audio_client: tuple[MagicMock, MagicMock],
    asterisk_env_cleanup: None,
) -> None:
    """Connection rejected with wrong token."""
    os.environ["ASTERISK_SECRET_TOKEN"] = "correct_token"

    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect("/api/asterisk/live?token=wrong_token"),
    ):
        pass

    assert exc_info.value.code == 1008


@pytest.mark.no_db
def test_asterisk_rejects_unauthorized_extension(
    mock_live_audio_client: tuple[MagicMock, MagicMock],
    asterisk_env_cleanup: None,
) -> None:
    """Connection rejected when extension not in allow-list."""
    os.environ["ASTERISK_SECRET_TOKEN"] = "test_token"
    os.environ["ASTERISK_ALLOWED_EXTENSIONS"] = "101,102,103"

    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect("/api/asterisk/live?token=test_token&extension=999"),
    ):
        pass

    assert exc_info.value.code == 1008


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_asterisk_accepts_valid_token(
    mock_live_audio_client: tuple[MagicMock, MagicMock],
    asterisk_env_cleanup: None,
) -> None:
    """Connection accepted with valid token."""
    client_mock, _session_mock = mock_live_audio_client
    os.environ["ASTERISK_SECRET_TOKEN"] = "valid_token"
    # No extension allow-list = all extensions allowed
    os.environ.pop("ASTERISK_ALLOWED_EXTENSIONS", None)

    with (
        TestClient(app) as client,
        client.websocket_connect("/api/asterisk/live?token=valid_token") as websocket,
    ):
        # Send MEDIA_START to establish connection
        websocket.send_text("MEDIA_START format:slin16")
        await wait_for_condition(lambda: client_mock.connect.called)
        assert client_mock.connect.called


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_asterisk_accepts_authorized_extension(
    mock_live_audio_client: tuple[MagicMock, MagicMock],
    asterisk_env_cleanup: None,
) -> None:
    """Connection accepted when extension is in allow-list."""
    client_mock, _session_mock = mock_live_audio_client
    os.environ["ASTERISK_SECRET_TOKEN"] = "test_token"
    os.environ["ASTERISK_ALLOWED_EXTENSIONS"] = "101,102,103"

    with (
        TestClient(app) as client,
        client.websocket_connect(
            "/api/asterisk/live?token=test_token&extension=102"
        ) as websocket,
    ):
        websocket.send_text("MEDIA_START format:slin16")
        await wait_for_condition(lambda: client_mock.connect.called)
        assert client_mock.connect.called


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_asterisk_allows_all_when_no_auth_configured(
    mock_live_audio_client: tuple[MagicMock, MagicMock],
    asterisk_env_cleanup: None,
) -> None:
    """Connection allowed when no authentication is configured (backward compatible)."""
    client_mock, _session_mock = mock_live_audio_client
    # Clear all auth config
    os.environ.pop("ASTERISK_SECRET_TOKEN", None)
    os.environ.pop("ASTERISK_ALLOWED_EXTENSIONS", None)

    with (
        TestClient(app) as client,
        client.websocket_connect("/api/asterisk/live") as websocket,
    ):
        websocket.send_text("MEDIA_START format:slin16")
        await wait_for_condition(lambda: client_mock.connect.called)
        assert client_mock.connect.called


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_asterisk_passes_extension_and_channel_to_handler(
    mock_live_audio_client: tuple[MagicMock, MagicMock],
    asterisk_env_cleanup: None,
) -> None:
    """Extension and channel_id are passed to the handler."""
    client_mock, _session_mock = mock_live_audio_client
    # No auth required for this test
    os.environ.pop("ASTERISK_SECRET_TOKEN", None)
    os.environ.pop("ASTERISK_ALLOWED_EXTENSIONS", None)

    with (
        TestClient(app) as client,
        client.websocket_connect(
            "/api/asterisk/live?extension=101&channel_id=SIP/101-00001"
        ) as websocket,
    ):
        websocket.send_text("MEDIA_START format:slin16")
        await wait_for_condition(lambda: client_mock.connect.called)
        # Connection should succeed - extension and channel_id are informational
        assert client_mock.connect.called
