import asyncio
import struct
import wave
from array import array
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import pytest
from google.genai.types import (
    Blob,
    Content,
    LiveServerContent,
    LiveServerMessage,
    Part,
)
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.websockets import WebSocketState

from family_assistant.paths import WEB_RESOURCES_DIR
from family_assistant.security.note_provenance import NoteProvenanceStamp
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.notes import NoteReadPolicy, NoteWritePolicy
from family_assistant.web.models import GeminiLiveConfig, GeminiLiveGreetingConfig
from family_assistant.web.routers.asterisk_live_api import AsteriskLiveHandler
from tests.mocks.idle_processing_service import make_idle_processing_service

if TYPE_CHECKING:
    from fastapi import WebSocket
    from google.genai.live import AsyncSession

    from family_assistant.processing import ProcessingService
    from family_assistant.web.audio_utils import StatefulResampler
    from family_assistant.web.voice_client import LiveAudioClient


class FakeAsteriskWebSocket:
    """Stands in for the Asterisk side of the websocket.

    Serves scripted inbound messages and records everything sent back.
    """

    def __init__(self, inbound: Sequence[dict[str, object]] = ()) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.inbound = list(inbound)
        self.receive_count = 0
        self.sent_bytes: list[bytes] = []
        self.sent_text: list[str] = []

    async def accept(self) -> None:
        return None

    async def close(self, code: int = 1000) -> None:
        self.client_state = WebSocketState.DISCONNECTED

    async def receive(self) -> dict[str, object]:
        self.receive_count += 1
        assert self.inbound, "the handler read past the scripted messages"
        return self.inbound.pop(0)

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def send_text(self, data: str) -> None:
        self.sent_text.append(data)

    def as_websocket(self) -> "WebSocket":
        return cast("WebSocket", self)


class FakeLiveSession:
    """A Gemini Live session that records sent input and replays scripted responses once."""

    def __init__(self, responses: Sequence[LiveServerMessage] = ()) -> None:
        self._responses = list(responses)
        self.realtime_inputs: list[dict[str, object]] = []

    async def send_realtime_input(self, **kwargs: object) -> None:
        self.realtime_inputs.append(kwargs)

    async def send_tool_response(self, **kwargs: object) -> None:
        raise AssertionError(f"No tool call was made, got a response: {kwargs}")

    async def receive(self) -> AsyncIterator[LiveServerMessage]:
        responses, self._responses = self._responses, []
        for response in responses:
            yield response

    def as_session(self) -> "AsyncSession":
        return cast("AsyncSession", self)


class FakeLiveClient:
    """Stands in for ``GoogleGeminiLiveClient``, handing out one session."""

    def __init__(
        self,
        session: FakeLiveSession,
        on_connect: Callable[[], None] | None = None,
    ) -> None:
        self._session = session
        self._on_connect = on_connect
        self.connect_configs: list[object] = []

    @asynccontextmanager
    async def connect(self, config: object) -> AsyncGenerator[FakeLiveSession]:
        self.connect_configs.append(config)
        if self._on_connect is not None:
            self._on_connect()
        yield self._session

    def as_client(self) -> "LiveAudioClient":
        return cast("LiveAudioClient", self)


class FixedResampler:
    """A resampler that records its input and answers with fixed output."""

    def __init__(self, output: bytes) -> None:
        self._output = output
        self.inputs: list[bytes] = []

    def resample(self, data: bytes) -> bytes:
        self.inputs.append(data)
        return self._output


@pytest.fixture
def websocket() -> FakeAsteriskWebSocket:
    return FakeAsteriskWebSocket()


@pytest.fixture
def live_config() -> GeminiLiveConfig:
    return GeminiLiveConfig(greeting=GeminiLiveGreetingConfig(enabled=True))


@pytest.fixture
def handler(
    websocket: FakeAsteriskWebSocket, live_config: GeminiLiveConfig
) -> AsteriskLiveHandler:
    h = AsteriskLiveHandler(
        websocket=websocket.as_websocket(),
        client=FakeLiveClient(FakeLiveSession()).as_client(),
        gemini_live_config=live_config,
    )
    h.format = "slin16"
    return h


def _make_gemini_response(raw_audio: bytes) -> LiveServerMessage:
    blob = Blob(data=raw_audio, mime_type="audio/pcm;rate=24000")
    return LiveServerMessage(
        server_content=LiveServerContent(
            model_turn=Content(parts=[Part(inline_data=blob)])
        )
    )


def _single_response_session(response: LiveServerMessage) -> "AsyncSession":
    return FakeLiveSession([response]).as_session()


def _transcript_service(labels: list[str]) -> "ProcessingService":
    service = make_idle_processing_service()
    service.service_config = replace(
        service.service_config, default_note_visibility_labels=labels
    )
    return service


class TestReceiveFromGemini:
    async def test_forwards_audio(
        self, handler: AsteriskLiveHandler, websocket: FakeAsteriskWebSocket
    ) -> None:
        """Audio received from Gemini is forwarded as the resampled output, not the raw input."""
        raw_audio = b"\x01\x02\x03\x04" * 100
        resampled_audio = b"\x09\x08" * 160  # one full send frame (320 bytes)
        handler.gemini_session = _single_response_session(
            _make_gemini_response(raw_audio)
        )
        resampler = FixedResampler(resampled_audio)
        handler.gemini_to_asterisk_resampler = cast("StatefulResampler", resampler)

        await handler._receive_from_gemini()

        assert resampler.inputs == [raw_audio]
        assert b"".join(websocket.sent_bytes) == resampled_audio

    async def test_handles_invalid_format_gracefully(
        self, handler: AsteriskLiveHandler, websocket: FakeAsteriskWebSocket
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
        assert len(websocket.sent_bytes) == 3
        assert b"".join(websocket.sent_bytes) == raw_audio

    async def test_respects_flow_control(
        self, handler: AsteriskLiveHandler, websocket: FakeAsteriskWebSocket
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
        assert websocket.sent_bytes == []

        # Simulate MEDIA_XON — unblock the sender
        handler.media_send_allowed.set()
        await task

        # After unblocking, bytes should have been sent
        assert len(websocket.sent_bytes) > 0


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

        assert list(output_samples) == [16383, -16384]

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
        self,
        handler: AsteriskLiveHandler,
        websocket: FakeAsteriskWebSocket,
        live_config: GeminiLiveConfig,
        tmp_path: Path,
    ) -> None:
        """Pre-canned greeting sends audio frames to WebSocket."""
        live_config.greeting.wav_path = str(
            _make_wav_file(tmp_path, rate=16000, num_frames=1600)
        )

        await handler._send_precanned_greeting()

        # 1600 frames * 2 bytes = 3200 bytes total, frame_size = 640 -> 5 frames
        assert len(websocket.sent_bytes) == 5
        assert len(b"".join(websocket.sent_bytes)) == 3200

    async def test_sends_audio_with_real_path(
        self, handler: AsteriskLiveHandler, websocket: FakeAsteriskWebSocket
    ) -> None:
        """Greeting sends audio using the actual greeting.wav resource."""
        greeting_path = WEB_RESOURCES_DIR / "greeting.wav"
        with wave.open(str(greeting_path), "rb") as wf:
            total_bytes = wf.getnframes() * wf.getsampwidth()

        await handler._send_precanned_greeting()

        calls = websocket.sent_bytes
        expected_frames = -(-total_bytes // handler.send_frame_size)  # ceil division
        assert len(calls) == expected_frames
        assert len(b"".join(calls)) == total_bytes
        assert all(len(frame) <= handler.send_frame_size for frame in calls)

    async def test_resamples_when_rate_differs(
        self, handler: AsteriskLiveHandler, websocket: FakeAsteriskWebSocket
    ) -> None:
        """Greeting resamples WAV when Asterisk rate differs from WAV rate."""
        handler.sample_rate = 8000
        handler.send_frame_size = 320  # 20ms at 8kHz

        greeting_path = WEB_RESOURCES_DIR / "greeting.wav"
        with wave.open(str(greeting_path), "rb") as wf:
            original_bytes = wf.getnframes() * wf.getsampwidth()

        await handler._send_precanned_greeting()

        sent_bytes = sum(len(frame) for frame in websocket.sent_bytes)
        expected = original_bytes // 2
        assert abs(sent_bytes - expected) < expected * 0.05

    async def test_handles_missing_file(
        self,
        handler: AsteriskLiveHandler,
        websocket: FakeAsteriskWebSocket,
        live_config: GeminiLiveConfig,
        tmp_path: Path,
    ) -> None:
        """Missing greeting.wav is handled gracefully."""
        live_config.greeting.wav_path = str(tmp_path / "missing.wav")

        await handler._send_precanned_greeting()

        assert websocket.sent_bytes == []


class TestMediaRelay:
    """Tests for relaying caller audio to Gemini."""

    async def test_audio_sent_to_connected_session(
        self, handler: AsteriskLiveHandler
    ) -> None:
        """Audio goes directly to Gemini as the sent audio blob."""
        session = FakeLiveSession()
        handler.gemini_session = session.as_session()
        raw_audio = b"\x01\x02" * 100

        await handler._handle_media_message(raw_audio)

        assert len(session.realtime_inputs) == 1
        sent_blob = session.realtime_inputs[0]["audio"]
        assert isinstance(sent_blob, Blob)
        assert sent_blob.data == raw_audio
        assert sent_blob.mime_type == "audio/pcm;rate=16000"


class TestGeminiConnectFlow:
    """Exercises handler.run() end-to-end against a fake Gemini client.

    Covers behaviour that only happens inside the connect step of run() and
    cannot be observed by calling handler methods directly: caller audio sent
    right after MEDIA_START reaching the newly-connected session, and the
    greeting being (or not being) played while connecting.
    """

    @staticmethod
    def _connect_then_disconnect_messages() -> list[dict[str, object]]:
        return [
            {
                "type": "websocket.receive",
                "text": (
                    "MEDIA_START connection_id:test format:slin16 "
                    "optimal_frame_size:320"
                ),
            },
            {"type": "websocket.disconnect"},
        ]

    async def test_audio_following_media_start_reaches_gemini_in_order(
        self,
    ) -> None:
        """Caller audio sent straight after MEDIA_START is relayed to Gemini, in order.

        run() does not read the websocket while Gemini connects, so these
        frames wait in the transport and are relayed once the session is up.
        """
        chunk1 = b"\x01\x02" * 100
        chunk2 = b"\x03\x04" * 100
        media_start, disconnect = self._connect_then_disconnect_messages()
        websocket = FakeAsteriskWebSocket([
            media_start,
            {"type": "websocket.receive", "bytes": chunk1},
            {"type": "websocket.receive", "bytes": chunk2},
            disconnect,
        ])
        session = FakeLiveSession()
        receive_calls_before_connect: list[int] = []
        client = FakeLiveClient(
            session,
            on_connect=lambda: receive_calls_before_connect.append(
                websocket.receive_count
            ),
        )
        config = GeminiLiveConfig(greeting=GeminiLiveGreetingConfig(enabled=False))
        handler = AsteriskLiveHandler(
            websocket=websocket.as_websocket(),
            client=client.as_client(),
            gemini_live_config=config,
        )
        handler.database_engine = None

        await handler.run()

        assert receive_calls_before_connect == [1]
        sent_blobs = [inputs["audio"] for inputs in session.realtime_inputs]
        assert all(isinstance(blob, Blob) for blob in sent_blobs)
        blobs = cast("list[Blob]", sent_blobs)
        assert [blob.data for blob in blobs] == [chunk1, chunk2]
        assert all(blob.mime_type == "audio/pcm;rate=16000" for blob in blobs)

    async def test_greeting_not_sent_when_disabled(self) -> None:
        """No greeting frames reach the websocket when greeting.enabled is False."""
        websocket = FakeAsteriskWebSocket(self._connect_then_disconnect_messages())
        client = FakeLiveClient(FakeLiveSession())
        config = GeminiLiveConfig(greeting=GeminiLiveGreetingConfig(enabled=False))
        handler = AsteriskLiveHandler(
            websocket=websocket.as_websocket(),
            client=client.as_client(),
            gemini_live_config=config,
        )
        handler.database_engine = None

        await handler.run()

        assert "ANSWER" in websocket.sent_text
        assert len(client.connect_configs) == 1
        assert websocket.sent_bytes == []


class TestCallTranscriptSaving:
    """Tests for call transcript accumulation and saving."""

    @pytest.fixture(autouse=True)
    def _setup_transcript(self, handler: AsteriskLiveHandler) -> None:
        handler.extension = "100"

    async def test_no_segments_writes_no_note(
        self, handler: AsteriskLiveHandler, db_engine: AsyncEngine
    ) -> None:
        """An empty call (no transcript segments) should not create any note."""
        handler.database_engine = db_engine
        handler._transcript_segments = []

        await handler._save_call_transcript()

        db = Database(db_engine)
        notes = await db.notes.get_all(read_policy=NoteReadPolicy.UNRESTRICTED)
        assert notes == []

    async def test_flushes_partial_caller_buffer(
        self, handler: AsteriskLiveHandler, db_engine: AsyncEngine
    ) -> None:
        """Partial caller buffer text should reach the saved transcript note."""
        handler.database_engine = db_engine
        handler._caller_transcript_buf = ["Hello", " world"]
        handler._assistant_transcript_buf = []
        handler._transcript_segments = []

        await handler._save_call_transcript()

        assert handler._caller_transcript_buf == []
        db = Database(db_engine)
        notes = await db.notes.get_all(read_policy=NoteReadPolicy.UNRESTRICTED)
        assert len(notes) == 1
        assert "Caller: Hello world" in notes[0].content

    async def test_flushes_partial_assistant_buffer(
        self, handler: AsteriskLiveHandler, db_engine: AsyncEngine
    ) -> None:
        """Partial assistant buffer text should reach the saved transcript note."""
        handler.database_engine = db_engine
        handler._caller_transcript_buf = []
        handler._assistant_transcript_buf = ["Good", "bye"]
        handler._transcript_segments = []

        await handler._save_call_transcript()

        assert handler._assistant_transcript_buf == []
        db = Database(db_engine)
        notes = await db.notes.get_all(read_policy=NoteReadPolicy.UNRESTRICTED)
        assert len(notes) == 1
        assert "Assistant: Goodbye" in notes[0].content

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

        db = Database(db_engine)
        notes = await db.notes.get_all(read_policy=NoteReadPolicy.UNRESTRICTED)
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

        handler.processing_service = _transcript_service(["telephone_logs"])

        await handler._save_call_transcript()

        db = Database(db_engine)
        notes = await db.notes.get_all(read_policy=NoteReadPolicy.UNRESTRICTED)
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

        handler.processing_service = _transcript_service(["telephone_logs"])

        call_time = datetime.fromtimestamp(handler._call_start_time, tz=ZoneInfo("UTC"))
        title = f"Call Transcript: 100 - {call_time.strftime('%Y-%m-%d %H:%M')}"
        db = Database(db_engine)
        await db.notes.add_or_update(
            title=title,
            content="pre-fix transcript",
            visibility_labels=[],
            write_policy=NoteWritePolicy.UNCONSTRAINED,
            provenance=NoteProvenanceStamp.internal(),
        )

        await handler._save_call_transcript()

        db = Database(db_engine)
        note = await db.notes.get_by_title(
            title, read_policy=NoteReadPolicy.UNRESTRICTED
        )
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

        db = Database(db_engine)
        notes = await db.notes.get_all(read_policy=NoteReadPolicy.UNRESTRICTED)
        assert len(notes) == 1
        note = notes[0]

        assert "Call Transcript: 100 -" in note.title
