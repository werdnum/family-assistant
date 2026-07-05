import asyncio
import struct
import tempfile
import wave
from array import array
from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from google.genai.types import Blob, Part
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.websockets import WebSocketState

from family_assistant.paths import WEB_RESOURCES_DIR
from family_assistant.storage.context import get_db_context
from family_assistant.storage.repositories.notes import NoteWritePolicy
from family_assistant.web.routers.asterisk_live_api import AsteriskLiveHandler


@pytest.fixture
def mock_websocket() -> AsyncMock:
    ws = AsyncMock()
    ws.client_state = WebSocketState.CONNECTED
    return ws


@pytest.fixture
def mock_config() -> MagicMock:
    config = MagicMock()
    config.vad = MagicMock(automatic=True)
    config.greeting = MagicMock(enabled=True, wav_path=None)
    return config


@pytest.fixture
def handler(mock_websocket: AsyncMock, mock_config: MagicMock) -> AsteriskLiveHandler:
    h = AsteriskLiveHandler(
        websocket=mock_websocket,
        client=MagicMock(),
        gemini_live_config=mock_config,
    )
    h.format = "slin16"
    return h


def _make_gemini_response(raw_audio: bytes) -> MagicMock:
    blob = Blob(data=raw_audio, mime_type="audio/pcm;rate=24000")
    part = Part(inline_data=blob)
    resp = MagicMock()
    resp.server_content.model_turn.parts = [part]
    resp.server_content.input_transcription = None
    resp.server_content.output_transcription = None
    resp.tool_call = None
    return resp


def _single_response_session(response: MagicMock) -> MagicMock:
    async def mock_receive() -> AsyncGenerator[MagicMock]:
        yield response

    session = MagicMock()
    session.receive.return_value = mock_receive()
    return session


class TestReceiveFromGemini:
    async def test_forwards_audio(
        self, handler: AsteriskLiveHandler, mock_websocket: AsyncMock
    ) -> None:
        """Audio received from Gemini is correctly forwarded to Asterisk."""
        raw_audio = b"\x01\x02\x03\x04" * 100
        handler.gemini_session = _single_response_session(
            _make_gemini_response(raw_audio)
        )
        handler.gemini_to_asterisk_resampler = MagicMock()
        handler.gemini_to_asterisk_resampler.resample.return_value = raw_audio
        handler.optimal_frame_size = 10

        await handler._receive_from_gemini()

        assert mock_websocket.send_bytes.called
        sent_data = mock_websocket.send_bytes.call_args[0][0]
        assert isinstance(sent_data, bytes)
        assert len(sent_data) > 0

    async def test_handles_invalid_format_gracefully(
        self, handler: AsteriskLiveHandler, mock_websocket: AsyncMock
    ) -> None:
        """Valid-but-odd audio data doesn't crash the handler."""
        raw_audio = b"\x00\x01\x02"
        handler.gemini_session = _single_response_session(
            _make_gemini_response(raw_audio)
        )
        handler.gemini_to_asterisk_resampler = None
        handler.optimal_frame_size = 1
        handler.send_frame_size = 1
        handler.send_frame_duration_ms = 0.0

        await handler._receive_from_gemini()

        assert handler.audio_buffer == bytearray()
        calls = mock_websocket.send_bytes.call_args_list
        assert len(calls) == 3
        sent_data = b"".join(c[0][0] for c in calls)
        assert sent_data == raw_audio

    async def test_respects_flow_control(
        self, handler: AsteriskLiveHandler, mock_websocket: AsyncMock
    ) -> None:
        """MEDIA_XOFF halts sends until MEDIA_XON is received."""
        raw_audio = b"\x00\x01" * 50
        handler.gemini_session = _single_response_session(
            _make_gemini_response(raw_audio)
        )
        handler.gemini_to_asterisk_resampler = None
        handler.optimal_frame_size = 20
        handler.send_frame_size = 20
        handler.send_frame_duration_ms = 0.0

        # Simulate MEDIA_XOFF before audio arrives
        handler.media_send_allowed.clear()

        # Start the receive task — it should block on media_send_allowed.wait()
        task = asyncio.create_task(handler._receive_from_gemini())

        # Yield control so the task runs up to the wait point
        # ast-grep-ignore: no-asyncio-sleep-in-tests - Yielding event loop, not waiting
        await asyncio.sleep(0)
        # ast-grep-ignore: no-asyncio-sleep-in-tests - Yielding event loop, not waiting
        await asyncio.sleep(0)

        # No bytes should have been sent while flow control is paused
        assert not mock_websocket.send_bytes.called

        # Simulate MEDIA_XON — unblock the sender
        handler.media_send_allowed.set()
        await task

        # After unblocking, bytes should have been sent
        assert len(mock_websocket.send_bytes.call_args_list) > 0


class TestApplyDucking:
    def test_attenuates_audio(self, handler: AsteriskLiveHandler) -> None:
        handler.assistant_duck_gain = 0.5
        handler.format = "slin"
        samples = array("h", [1000, -1000])

        output = handler._apply_ducking(samples.tobytes())
        output_samples = array("h")
        output_samples.frombytes(output)

        assert list(output_samples) == [500, -500]

    def test_preserves_bounds(self, handler: AsteriskLiveHandler) -> None:
        handler.assistant_duck_gain = 0.5
        handler.format = "slin16"
        samples = array("h", [32767, -32768])

        output = handler._apply_ducking(samples.tobytes())
        output_samples = array("h")
        output_samples.frombytes(output)

        assert all(-32768 <= s <= 32767 for s in output_samples)

    def test_handles_odd_length(self, handler: AsteriskLiveHandler) -> None:
        handler.assistant_duck_gain = 0.5
        handler.format = "slin"

        output = handler._apply_ducking(b"\x01\x02\x03")

        assert len(output) == 2

    def test_skips_non_linear_formats(self, handler: AsteriskLiveHandler) -> None:
        handler.assistant_duck_gain = 0.5
        audio_data = b"\x00\x01\x02\x03"

        handler.format = "ulaw"
        assert handler._apply_ducking(audio_data) == audio_data

        handler.format = "alaw"
        assert handler._apply_ducking(audio_data) == audio_data


def _make_wav_file(tmp_path: Path, rate: int = 16000, num_frames: int = 1600) -> Path:
    """Create a test WAV file with known content."""
    wav_path = tmp_path / "greeting.wav"
    samples = struct.pack(f"<{num_frames}h", *([1000] * num_frames))
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples)
    return wav_path


class TestPrecannedGreeting:
    """Tests for the pre-canned greeting feature."""

    @pytest.fixture(autouse=True)
    def _setup_greeting(self, handler: AsteriskLiveHandler) -> None:
        handler.sample_rate = 16000
        handler.send_frame_size = 640  # 20ms at 16kHz
        handler.send_frame_duration_ms = 0.0  # No pacing delay in tests

    async def test_sends_audio_to_websocket(
        self, handler: AsteriskLiveHandler, mock_websocket: AsyncMock
    ) -> None:
        """Pre-canned greeting sends audio frames to WebSocket."""
        with tempfile.TemporaryDirectory() as tmpdir:
            resources_dir = Path(tmpdir)
            _make_wav_file(resources_dir, rate=16000, num_frames=1600)

            with patch(
                "family_assistant.web.routers.asterisk_live_api.WEB_RESOURCES_DIR",
                resources_dir,
            ):
                await handler._send_precanned_greeting()

        assert mock_websocket.send_bytes.called
        calls = mock_websocket.send_bytes.call_args_list
        # 1600 frames * 2 bytes = 3200 bytes total, frame_size = 640 -> 5 frames
        assert len(calls) == 5
        sent_data = b"".join(c[0][0] for c in calls)
        assert len(sent_data) == 3200

    async def test_sends_audio_with_real_path(
        self, handler: AsteriskLiveHandler, mock_websocket: AsyncMock
    ) -> None:
        """Greeting sends audio using the actual greeting.wav resource."""
        greeting_path = WEB_RESOURCES_DIR / "greeting.wav"
        if not greeting_path.exists():
            pytest.skip("greeting.wav not found")

        await handler._send_precanned_greeting()

        assert mock_websocket.send_bytes.called
        for call in mock_websocket.send_bytes.call_args_list:
            assert isinstance(call[0][0], bytes)

    async def test_not_sent_when_disabled(self, mock_config: MagicMock) -> None:
        """No greeting is sent when greeting.enabled = False."""
        mock_config.greeting.enabled = False
        assert not mock_config.greeting.enabled

    async def test_resamples_when_rate_differs(
        self, handler: AsteriskLiveHandler, mock_websocket: AsyncMock
    ) -> None:
        """Greeting resamples WAV when Asterisk rate differs from WAV rate."""
        handler.sample_rate = 8000
        handler.send_frame_size = 320  # 20ms at 8kHz

        greeting_path = WEB_RESOURCES_DIR / "greeting.wav"
        if not greeting_path.exists():
            pytest.skip("greeting.wav not found")

        await handler._send_precanned_greeting()
        assert mock_websocket.send_bytes.called

    async def test_handles_missing_file(
        self, handler: AsteriskLiveHandler, mock_websocket: AsyncMock
    ) -> None:
        """Missing greeting.wav is handled gracefully."""
        with patch(
            "family_assistant.web.routers.asterisk_live_api.pathlib.Path.exists",
            return_value=False,
        ):
            await handler._send_precanned_greeting()

        assert not mock_websocket.send_bytes.called


class TestPreGeminiAudioBuffering:
    """Tests for audio buffering before Gemini session is established."""

    async def test_audio_buffered_before_gemini_connects(
        self, handler: AsteriskLiveHandler
    ) -> None:
        """Audio received before Gemini connects is buffered."""
        handler.gemini_session = None

        audio_chunk_1 = b"\x01\x02" * 100
        audio_chunk_2 = b"\x03\x04" * 100

        await handler._handle_media_message(audio_chunk_1)
        await handler._handle_media_message(audio_chunk_2)

        assert len(handler._audio_buffer_pre_gemini) == 2
        assert handler._audio_buffer_pre_gemini[0] == audio_chunk_1
        assert handler._audio_buffer_pre_gemini[1] == audio_chunk_2

    async def test_audio_not_buffered_after_gemini_connects(
        self, handler: AsteriskLiveHandler
    ) -> None:
        """Audio goes directly to Gemini after session is established."""
        mock_session = AsyncMock()
        handler.gemini_session = mock_session

        await handler._handle_media_message(b"\x01\x02" * 100)

        assert len(handler._audio_buffer_pre_gemini) == 0
        mock_session.send_realtime_input.assert_called_once()

    async def test_buffer_starts_empty(self, handler: AsteriskLiveHandler) -> None:
        """Pre-Gemini audio buffer is empty on init."""
        assert len(handler._audio_buffer_pre_gemini) == 0


class TestCallTranscriptSaving:
    """Tests for call transcript accumulation and saving."""

    @pytest.fixture(autouse=True)
    def _setup_transcript(self, handler: AsteriskLiveHandler) -> None:
        handler.extension = "100"

    async def test_no_segments_no_db(self, handler: AsteriskLiveHandler) -> None:
        """No segments and no database engine should be a no-op."""
        handler.database_engine = None
        handler._transcript_segments = []
        await handler._save_call_transcript()

    async def test_flushes_partial_caller_buffer(
        self, handler: AsteriskLiveHandler
    ) -> None:
        """Partial caller buffer should be flushed into segments before save check."""
        handler.database_engine = None
        handler._caller_transcript_buf = ["Hello", " world"]
        handler._assistant_transcript_buf = []
        handler._transcript_segments = []

        await handler._save_call_transcript()

        assert len(handler._transcript_segments) == 1
        assert handler._transcript_segments[0][0] == "Caller"
        assert handler._transcript_segments[0][1] == "Hello world"
        assert handler._caller_transcript_buf == []

    async def test_flushes_partial_assistant_buffer(
        self, handler: AsteriskLiveHandler
    ) -> None:
        """Partial assistant buffer should be flushed into segments before save check."""
        handler.database_engine = None
        handler._caller_transcript_buf = []
        handler._assistant_transcript_buf = ["Good", "bye"]
        handler._transcript_segments = []

        await handler._save_call_transcript()

        assert len(handler._transcript_segments) == 1
        assert handler._transcript_segments[0][0] == "Assistant"
        assert handler._transcript_segments[0][1] == "Goodbye"
        assert handler._assistant_transcript_buf == []

    async def test_formats_timestamps(
        self, handler: AsteriskLiveHandler, db_engine: AsyncEngine
    ) -> None:
        """Transcript segments should be formatted with MM:SS timestamps."""
        handler.database_engine = db_engine
        handler._transcript_segments = [
            ("Caller", "Hello", 0.0),
            ("Assistant", "Hi there", 5.5),
            ("Caller", "Thanks", 65.3),
        ]

        await handler._save_call_transcript()

        async with get_db_context(db_engine) as db:
            notes = await db.notes.get_all(visibility_grants=None)
        assert len(notes) == 1
        note = notes[0]

        assert "[00:00] Caller: Hello" in note.content
        assert "[00:05] Assistant: Hi there" in note.content
        assert "[01:05] Caller: Thanks" in note.content
        assert note.include_in_prompt is False

    async def test_uses_visibility_labels_from_service(
        self, handler: AsteriskLiveHandler, db_engine: AsyncEngine
    ) -> None:
        """Visibility labels should come from the processing service config."""
        handler.database_engine = db_engine
        handler._transcript_segments = [("Caller", "Hello", 0.0)]

        mock_service = MagicMock()
        mock_service.service_config.default_note_visibility_labels = ["telephone_logs"]
        mock_service.service_config.visibility_grants = None
        mock_service.service_config.required_note_visibility_labels = None
        mock_service.service_config.allowed_note_visibility_labels = None
        mock_service.service_config.timezone = ZoneInfo("UTC")
        handler.processing_service = mock_service

        await handler._save_call_transcript()

        async with get_db_context(db_engine) as db:
            notes = await db.notes.get_all(visibility_grants=None)
        assert len(notes) == 1
        note = notes[0]

        assert note.visibility_labels == ["telephone_logs"]

    async def test_restamps_labels_when_overwriting_unlabeled_note(
        self, handler: AsteriskLiveHandler, db_engine: AsyncEngine
    ) -> None:
        """A title collision with an unlabeled note is re-stamped, not preserved.

        Omitted visibility_labels means "preserve existing" in the repository,
        so the writer passes the labels explicitly; otherwise a retry against a
        pre-fix unlabeled transcript would stay default-visible.
        """
        handler.database_engine = db_engine
        handler._transcript_segments = [("Caller", "Hello", 0.0)]

        mock_service = MagicMock()
        mock_service.service_config.default_note_visibility_labels = ["telephone_logs"]
        mock_service.service_config.timezone = ZoneInfo("UTC")
        handler.processing_service = mock_service

        call_time = datetime.fromtimestamp(handler._call_start_time, tz=ZoneInfo("UTC"))
        title = f"Call Transcript: 100 - {call_time.strftime('%Y-%m-%d %H:%M')}"
        async with get_db_context(db_engine) as db:
            await db.notes.add_or_update(
                title=title,
                content="pre-fix transcript",
                visibility_labels=[],
                write_policy=NoteWritePolicy.UNCONSTRAINED,
            )

        await handler._save_call_transcript()

        async with get_db_context(db_engine) as db:
            note = await db.notes.get_by_title(title, visibility_grants=None)
        assert note is not None
        assert "[00:00] Caller: Hello" in note.content
        assert note.visibility_labels == ["telephone_logs"]

    async def test_title_includes_extension(
        self, handler: AsteriskLiveHandler, db_engine: AsyncEngine
    ) -> None:
        """Title should include the extension and datetime."""
        handler.database_engine = db_engine
        handler._transcript_segments = [("Caller", "Hello", 0.0)]

        await handler._save_call_transcript()

        async with get_db_context(db_engine) as db:
            notes = await db.notes.get_all(visibility_grants=None)
        assert len(notes) == 1
        note = notes[0]

        assert "Call Transcript: 100 -" in note.title
