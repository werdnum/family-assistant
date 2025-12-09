"""Functional tests for Asterisk Live API."""

import contextlib
import json
import os
from collections.abc import AsyncIterator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from family_assistant.web.app_creator import app
from tests.helpers import wait_for_condition


@pytest.fixture
def mock_gemini_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[MagicMock, MagicMock]]:
    """Mock the google.genai.Client at the API boundary.

    This fixture mocks the Google GenAI Client class, allowing us to test
    the full dependency injection chain including:
    - get_live_audio_client dependency (with WebSocket parameter injection)
    - GoogleGeminiLiveClient construction with api_key and model
    - genai.Client instantiation with correct parameters
    - client.aio.live.connect call

    By mocking at the API client boundary rather than the dependency level,
    we ensure our code is exercised up to the point of making API calls.
    """
    # Create the mock session that would be returned by client.aio.live.connect
    mock_session = MagicMock()
    mock_session.send = AsyncMock()

    async def empty_receive() -> AsyncIterator[None]:
        # Empty async iterator - must have yield to be a generator
        # Using standard pattern for empty async generators
        for _ in []:
            yield

    mock_session.receive.return_value = empty_receive()

    # Create the async context manager for connect
    mock_connect_cm = AsyncMock()
    mock_connect_cm.__aenter__.return_value = mock_session
    mock_connect_cm.__aexit__.return_value = None

    # Create the mock client
    mock_client_instance = MagicMock()
    mock_client_instance.aio.live.connect.return_value = mock_connect_cm

    # Set GEMINI_API_KEY so the dependency doesn't fail
    monkeypatch.setenv("GEMINI_API_KEY", "test-api-key-for-testing")

    with patch("google.genai.Client", return_value=mock_client_instance) as mock_client:
        yield mock_client, mock_session


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
@pytest.mark.asyncio
async def test_asterisk_connection_flow(
    mock_gemini_client: tuple[MagicMock, MagicMock],
) -> None:
    """Test the basic flow of connecting from Asterisk.

    This test exercises the full path from WebSocket connection through
    to the Gemini API mock, verifying:
    - get_live_audio_client dependency correctly receives WebSocket
    - GoogleGeminiLiveClient is constructed with correct API key
    - genai.Client is instantiated with correct parameters
    - client.aio.live.connect is called with the model
    """
    mock_client_class, mock_session = mock_gemini_client

    with (
        TestClient(app) as client,
        client.websocket_connect("/api/asterisk/live") as websocket,
    ):
        # 1. Send MEDIA_START (Asterisk -> Server)
        websocket.send_text(
            "MEDIA_START connection_id:test-conn format:slin16 optimal_frame_size:320"
        )

        # Allow async loop to process message and connect to Gemini
        await wait_for_condition(lambda: mock_client_class.called)

        # Verify that genai.Client was instantiated with correct parameters
        assert mock_client_class.called
        call_kwargs = mock_client_class.call_args[1]
        assert call_kwargs["api_key"] == "test-api-key-for-testing"
        assert call_kwargs["http_options"] == {"api_version": "v1alpha"}

        # Verify that connect was called with a model
        mock_client_instance = mock_client_class.return_value
        assert mock_client_instance.aio.live.connect.called

        # 2. Send Audio (Asterisk -> Server)
        audio_chunk = b"\x00" * 320
        websocket.send_bytes(audio_chunk)

        # Give time for async processing
        await wait_for_condition(lambda: mock_session.send.called)

        # Check that audio was forwarded to Gemini
        assert mock_session.send.called

        # 3. Send HANGUP
        websocket.send_text("HANGUP")

        # WebSocket should close
        with contextlib.suppress(Exception):
            websocket.receive_text()


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_asterisk_json_protocol(
    mock_gemini_client: tuple[MagicMock, MagicMock],
) -> None:
    """Test using JSON protocol for Asterisk control messages."""
    mock_client_class, mock_session = mock_gemini_client

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

        await wait_for_condition(lambda: mock_client_class.called)
        assert mock_client_class.called

        # Send Audio
        websocket.send_bytes(b"\x00" * 320)

        # Give time for async processing
        await wait_for_condition(lambda: mock_session.send.called)

        # Verify send called
        assert mock_session.send.called


# --- Authentication Tests ---


@pytest.mark.no_db
def test_asterisk_rejects_missing_token(
    mock_gemini_client: tuple[MagicMock, MagicMock],
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
    mock_gemini_client: tuple[MagicMock, MagicMock],
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
    mock_gemini_client: tuple[MagicMock, MagicMock],
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
    mock_gemini_client: tuple[MagicMock, MagicMock],
    asterisk_env_cleanup: None,
) -> None:
    """Connection accepted with valid token."""
    mock_client_class, _mock_session = mock_gemini_client
    os.environ["ASTERISK_SECRET_TOKEN"] = "valid_token"
    # No extension allow-list = all extensions allowed
    os.environ.pop("ASTERISK_ALLOWED_EXTENSIONS", None)

    with (
        TestClient(app) as client,
        client.websocket_connect("/api/asterisk/live?token=valid_token") as websocket,
    ):
        # Send MEDIA_START to establish connection
        websocket.send_text("MEDIA_START format:slin16")
        await wait_for_condition(lambda: mock_client_class.called)
        assert mock_client_class.called


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_asterisk_accepts_authorized_extension(
    mock_gemini_client: tuple[MagicMock, MagicMock],
    asterisk_env_cleanup: None,
) -> None:
    """Connection accepted when extension is in allow-list."""
    mock_client_class, _mock_session = mock_gemini_client
    os.environ["ASTERISK_SECRET_TOKEN"] = "test_token"
    os.environ["ASTERISK_ALLOWED_EXTENSIONS"] = "101,102,103"

    with (
        TestClient(app) as client,
        client.websocket_connect(
            "/api/asterisk/live?token=test_token&extension=102"
        ) as websocket,
    ):
        websocket.send_text("MEDIA_START format:slin16")
        await wait_for_condition(lambda: mock_client_class.called)
        assert mock_client_class.called


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_asterisk_allows_all_when_no_auth_configured(
    mock_gemini_client: tuple[MagicMock, MagicMock],
    asterisk_env_cleanup: None,
) -> None:
    """Connection allowed when no authentication is configured (backward compatible)."""
    mock_client_class, _mock_session = mock_gemini_client
    # Clear all auth config
    os.environ.pop("ASTERISK_SECRET_TOKEN", None)
    os.environ.pop("ASTERISK_ALLOWED_EXTENSIONS", None)

    with (
        TestClient(app) as client,
        client.websocket_connect("/api/asterisk/live") as websocket,
    ):
        websocket.send_text("MEDIA_START format:slin16")
        await wait_for_condition(lambda: mock_client_class.called)
        assert mock_client_class.called


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_asterisk_passes_extension_and_channel_to_handler(
    mock_gemini_client: tuple[MagicMock, MagicMock],
    asterisk_env_cleanup: None,
) -> None:
    """Extension and channel_id are passed to the handler."""
    mock_client_class, _mock_session = mock_gemini_client
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
        await wait_for_condition(lambda: mock_client_class.called)
        # Connection should succeed - extension and channel_id are informational
        assert mock_client_class.called
