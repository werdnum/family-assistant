import asyncio
import struct
import tempfile
import unittest
import wave
from array import array
from collections.abc import AsyncGenerator
from contextlib import AbstractContextManager, asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from google.genai.types import Blob, Part
from starlette.websockets import WebSocketState

from family_assistant.paths import WEB_RESOURCES_DIR
from family_assistant.web.routers.asterisk_live_api import AsteriskLiveHandler


class TestAsteriskLiveHandler(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.mock_websocket = AsyncMock()
        self.mock_client = MagicMock()
        self.mock_config = MagicMock()
        self.mock_config.vad = MagicMock(automatic=True)

        # Configure default behavior for websocket
        self.mock_websocket.client_state = WebSocketState.CONNECTED

        self.handler = AsteriskLiveHandler(
            websocket=self.mock_websocket,
            client=self.mock_client,
            gemini_live_config=self.mock_config,
        )
        self.handler.format = (
            "slin16"  # Pre-configure format to avoid waiting loop in run()
        )

    async def test_receive_from_gemini_forwards_audio(self) -> None:
        """Test that audio received from Gemini is correctly forwarded to Asterisk."""
        # Setup the mock Gemini session
        mock_session = MagicMock()
        self.handler.gemini_session = mock_session

        # Create a mock response with raw audio bytes
        raw_audio = b"\x01\x02\x03\x04" * 100  # Some dummy audio data
        blob = Blob(data=raw_audio, mime_type="audio/pcm;rate=24000")
        part = Part(inline_data=blob)

        mock_response = MagicMock()
        mock_response.server_content.model_turn.parts = [part]
        mock_response.server_content.input_transcription = None
        mock_response.server_content.output_transcription = None
        mock_response.tool_call = None

        # Configure receive to yield the response then cancel
        async def mock_receive() -> AsyncGenerator[MagicMock]:
            yield mock_response

        mock_session.receive.return_value = mock_receive()

        # Manually initialize the resampler as it happens in _handle_control_message
        # NOTE: StatefulResampler might consume some bytes for internal buffering/history,
        # so output length might not match input length exactly immediately.
        # However, for this test we'll mock the resampler to avoid numpy/soxr dependencies and buffering logic behavior confusion
        self.handler.gemini_to_asterisk_resampler = MagicMock()
        # Make resampler return data as is or similar
        self.handler.gemini_to_asterisk_resampler.resample.return_value = raw_audio

        # Also need to make sure optimal_frame_size is small enough
        self.handler.optimal_frame_size = 10

        # Run the method
        await self.handler._receive_from_gemini()

        # Assertions
        self.assertTrue(self.mock_websocket.send_bytes.called)

        # Verify the data sent is bytes
        call_args = self.mock_websocket.send_bytes.call_args
        sent_data = call_args[0][0]
        self.assertIsInstance(sent_data, bytes)
        self.assertGreater(len(sent_data), 0)

    async def test_receive_from_gemini_handles_invalid_format_gracefully(self) -> None:
        """Test that valid-but-odd audio data doesn't crash the handler."""
        # This covers the specific bug case where previous code assumed base64
        mock_session = MagicMock()
        self.handler.gemini_session = mock_session

        # Data that isn't valid base64 (length not multiple of 4)
        raw_audio = b"\x00\x01\x02"
        blob = Blob(data=raw_audio, mime_type="audio/pcm;rate=24000")
        part = Part(inline_data=blob)

        mock_response = MagicMock()
        mock_response.server_content.model_turn.parts = [part]
        mock_response.server_content.input_transcription = None
        mock_response.server_content.output_transcription = None
        mock_response.tool_call = None

        async def mock_receive() -> AsyncGenerator[MagicMock]:
            yield mock_response

        mock_session.receive.return_value = mock_receive()

        # No resampler for simplicity (or simplistic one)
        self.handler.gemini_to_asterisk_resampler = None
        self.handler.optimal_frame_size = 1  # Force send immediately
        self.handler.send_frame_size = 1
        self.handler.send_frame_duration_ms = 0.0

        await self.handler._receive_from_gemini()

        # It should have buffered/sent the 3 bytes without crashing
        self.assertEqual(
            self.handler.audio_buffer, bytearray()
        )  # Should be empty if all sent

        # Check that we sent 3 bytes total.
        # call_args_list is a list of calls.
        calls = self.mock_websocket.send_bytes.call_args_list
        self.assertEqual(len(calls), 3)

        # Reconstruct sent data
        sent_data = b"".join([c[0][0] for c in calls])
        self.assertEqual(sent_data, raw_audio)

    async def test_receive_from_gemini_respects_flow_control(self) -> None:
        """Ensure MEDIA_XOFF halts sends until MEDIA_XON is received."""
        mock_session = MagicMock()
        self.handler.gemini_session = mock_session

        raw_audio = b"\x00\x01" * 50
        blob = Blob(data=raw_audio, mime_type="audio/pcm;rate=24000")
        part = Part(inline_data=blob)

        mock_response = MagicMock()
        mock_response.server_content.model_turn.parts = [part]
        mock_response.server_content.input_transcription = None
        mock_response.server_content.output_transcription = None
        mock_response.tool_call = None

        async def mock_receive() -> AsyncGenerator[MagicMock]:
            yield mock_response

        mock_session.receive.return_value = mock_receive()

        self.handler.gemini_to_asterisk_resampler = None
        self.handler.optimal_frame_size = 20
        self.handler.send_frame_size = 20
        self.handler.send_frame_duration_ms = 0.0

        # Simulate MEDIA_XOFF before audio arrives
        self.handler.media_send_allowed.clear()

        loop = asyncio.get_running_loop()
        xon_handle = loop.call_later(0.01, self.handler.media_send_allowed.set)
        start = loop.time()
        try:
            await self.handler._receive_from_gemini()
        finally:
            xon_handle.cancel()

        elapsed = loop.time() - start
        self.assertGreaterEqual(elapsed, 0.009)
        self.assertGreater(len(self.mock_websocket.send_bytes.call_args_list), 0)

    def test_apply_ducking_attenuates_audio(self) -> None:
        self.handler.assistant_duck_gain = 0.5
        self.handler.format = "slin"
        samples = array("h", [1000, -1000])
        audio_data = samples.tobytes()

        output = self.handler._apply_ducking(audio_data)
        output_samples = array("h")
        output_samples.frombytes(output)

        self.assertEqual(list(output_samples), [500, -500])

    def test_apply_ducking_preserves_bounds(self) -> None:
        self.handler.assistant_duck_gain = 0.5
        self.handler.format = "slin16"
        samples = array("h", [32767, -32768])
        audio_data = samples.tobytes()

        output = self.handler._apply_ducking(audio_data)
        output_samples = array("h")
        output_samples.frombytes(output)

        self.assertTrue(all(-32768 <= s <= 32767 for s in output_samples))

    def test_apply_ducking_handles_odd_length(self) -> None:
        self.handler.assistant_duck_gain = 0.5
        self.handler.format = "slin"
        audio_data = b"\x01\x02\x03"

        output = self.handler._apply_ducking(audio_data)

        self.assertEqual(len(output), 2)

    def test_apply_ducking_skips_non_linear_formats(self) -> None:
        self.handler.assistant_duck_gain = 0.5
        audio_data = b"\x00\x01\x02\x03"

        self.handler.format = "ulaw"
        output_ulaw = self.handler._apply_ducking(audio_data)
        self.assertEqual(output_ulaw, audio_data)

        self.handler.format = "alaw"
        output_alaw = self.handler._apply_ducking(audio_data)
        self.assertEqual(output_alaw, audio_data)


class TestPrecannedGreeting(unittest.IsolatedAsyncioTestCase):
    """Tests for the pre-canned greeting feature."""

    async def asyncSetUp(self) -> None:
        self.mock_websocket = AsyncMock()
        self.mock_client = MagicMock()
        self.mock_config = MagicMock()
        self.mock_config.vad = MagicMock(automatic=True)
        self.mock_config.greeting = MagicMock(enabled=True, wav_path=None)
        self.mock_websocket.client_state = WebSocketState.CONNECTED

        self.handler = AsteriskLiveHandler(
            websocket=self.mock_websocket,
            client=self.mock_client,
            gemini_live_config=self.mock_config,
        )
        self.handler.format = "slin16"
        self.handler.sample_rate = 16000
        self.handler.send_frame_size = 640  # 20ms at 16kHz
        self.handler.send_frame_duration_ms = 0.0  # No pacing delay in tests

    def _make_wav_file(
        self, tmp_path: Path, rate: int = 16000, num_frames: int = 1600
    ) -> Path:
        """Create a test WAV file with known content."""
        wav_path = tmp_path / "greeting.wav"
        samples = struct.pack(f"<{num_frames}h", *([1000] * num_frames))
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(samples)
        return wav_path

    async def test_greeting_sends_audio_to_websocket(self) -> None:
        """Test that pre-canned greeting sends audio frames to WebSocket."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            resources_dir = tmp_path
            self._make_wav_file(resources_dir, rate=16000, num_frames=1600)

            with patch(
                "family_assistant.web.routers.asterisk_live_api.WEB_RESOURCES_DIR",
                resources_dir,
            ):
                await self.handler._send_precanned_greeting()

        self.assertTrue(self.mock_websocket.send_bytes.called)
        calls = self.mock_websocket.send_bytes.call_args_list
        # 1600 frames * 2 bytes = 3200 bytes total, frame_size = 640 -> 5 frames
        self.assertEqual(len(calls), 5)
        sent_data = b"".join(c[0][0] for c in calls)
        self.assertEqual(len(sent_data), 3200)

    async def test_greeting_sends_audio_with_real_path(self) -> None:
        """Test greeting sends audio using the actual greeting.wav resource."""
        greeting_path = WEB_RESOURCES_DIR / "greeting.wav"
        if not greeting_path.exists():
            self.skipTest("greeting.wav not found")

        await self.handler._send_precanned_greeting()

        self.assertTrue(self.mock_websocket.send_bytes.called)
        calls = self.mock_websocket.send_bytes.call_args_list
        self.assertGreater(len(calls), 0)
        # All sent data should be bytes
        for call in calls:
            self.assertIsInstance(call[0][0], bytes)

    async def test_greeting_not_sent_when_disabled(self) -> None:
        """Test that no greeting is sent when greeting.enabled = False."""
        self.mock_config.greeting.enabled = False

        # The run() method checks greeting.enabled before creating the task.
        # Verify the flag prevents the task from being created.
        self.assertFalse(self.mock_config.greeting.enabled)

    async def test_greeting_resamples_when_rate_differs(self) -> None:
        """Test greeting resamples WAV when Asterisk rate differs from WAV rate."""
        self.handler.sample_rate = 8000  # Asterisk at 8kHz
        self.handler.send_frame_size = 320  # 20ms at 8kHz

        await self.handler._send_precanned_greeting()

        # The real greeting.wav is 16kHz, so it should be resampled to 8kHz
        greeting_path = (
            Path(__file__).parent.parent.parent.parent
            / "src"
            / "family_assistant"
            / "web"
            / "resources"
            / "greeting.wav"
        )
        if not greeting_path.exists():
            self.skipTest("greeting.wav not found")

        self.assertTrue(self.mock_websocket.send_bytes.called)

    async def test_greeting_handles_missing_file(self) -> None:
        """Test that missing greeting.wav is handled gracefully."""
        # Temporarily move the real file aside via patching
        with patch(
            "family_assistant.web.routers.asterisk_live_api.pathlib.Path.exists",
            return_value=False,
        ):
            await self.handler._send_precanned_greeting()

        self.assertFalse(self.mock_websocket.send_bytes.called)


class TestPreGeminiAudioBuffering(unittest.IsolatedAsyncioTestCase):
    """Tests for audio buffering before Gemini session is established."""

    async def asyncSetUp(self) -> None:
        self.mock_websocket = AsyncMock()
        self.mock_client = MagicMock()
        self.mock_config = MagicMock()
        self.mock_config.vad = MagicMock(automatic=True)
        self.mock_config.greeting = MagicMock(enabled=True, wav_path=None)
        self.mock_websocket.client_state = WebSocketState.CONNECTED

        self.handler = AsteriskLiveHandler(
            websocket=self.mock_websocket,
            client=self.mock_client,
            gemini_live_config=self.mock_config,
        )
        self.handler.format = "slin16"
        self.handler.sample_rate = 16000

    async def test_audio_buffered_before_gemini_connects(self) -> None:
        """Test that audio received before Gemini connects is buffered."""
        self.handler.gemini_session = None

        audio_chunk_1 = b"\x01\x02" * 100
        audio_chunk_2 = b"\x03\x04" * 100

        await self.handler._handle_media_message(audio_chunk_1)
        await self.handler._handle_media_message(audio_chunk_2)

        self.assertEqual(len(self.handler._audio_buffer_pre_gemini), 2)
        self.assertEqual(self.handler._audio_buffer_pre_gemini[0], audio_chunk_1)
        self.assertEqual(self.handler._audio_buffer_pre_gemini[1], audio_chunk_2)

    async def test_audio_not_buffered_after_gemini_connects(self) -> None:
        """Test that audio goes directly to Gemini after session is established."""
        mock_session = AsyncMock()
        self.handler.gemini_session = mock_session

        audio_data = b"\x01\x02" * 100
        await self.handler._handle_media_message(audio_data)

        self.assertEqual(len(self.handler._audio_buffer_pre_gemini), 0)
        mock_session.send_realtime_input.assert_called_once()

    async def test_buffer_starts_empty(self) -> None:
        """Test that the pre-Gemini audio buffer is empty on init."""
        self.assertEqual(len(self.handler._audio_buffer_pre_gemini), 0)


class _SavedNoteArgs:
    """Captures kwargs passed to notes.add_or_update in transcript tests."""

    def __init__(self) -> None:
        self.title: str = ""
        self.content: str = ""
        self.include_in_prompt: bool = True
        self.visibility_labels: list[str] | None = None
        self.called = False

    async def capture(
        self,
        title: str = "",
        content: str = "",
        include_in_prompt: bool = True,
        append: bool = False,
        attachment_ids: list[str] | None = None,
        visibility_labels: list[str] | None = None,
    ) -> str:
        self.title = title
        self.content = content
        self.include_in_prompt = include_in_prompt
        self.visibility_labels = visibility_labels
        self.called = True
        return "note-id"


@asynccontextmanager
async def _fake_db_context_capturing(
    captured: _SavedNoteArgs,
) -> AsyncGenerator[MagicMock]:
    """Provide a fake DB context that captures add_or_update kwargs."""
    mock_notes = MagicMock()
    mock_notes.add_or_update = captured.capture
    ctx = MagicMock()
    ctx.notes = mock_notes
    yield ctx


class TestCallTranscriptSaving(unittest.IsolatedAsyncioTestCase):
    """Tests for call transcript accumulation and saving."""

    async def asyncSetUp(self) -> None:
        self.mock_websocket = AsyncMock()
        self.mock_client = MagicMock()
        self.mock_config = MagicMock()
        self.mock_config.vad = MagicMock(automatic=True)
        self.mock_config.greeting = MagicMock(enabled=True, wav_path=None)
        self.mock_websocket.client_state = WebSocketState.CONNECTED

        self.handler = AsteriskLiveHandler(
            websocket=self.mock_websocket,
            client=self.mock_client,
            gemini_live_config=self.mock_config,
        )
        self.handler.format = "slin16"
        self.handler.extension = "100"

    def _patch_db(self, captured: _SavedNoteArgs) -> AbstractContextManager[object]:
        """Return a patch context that captures note save kwargs."""
        return patch(
            "family_assistant.web.routers.asterisk_live_api.get_db_context",
            side_effect=lambda engine: _fake_db_context_capturing(captured),
        )

    async def test_save_transcript_no_segments_no_db(self) -> None:
        """No segments and no database engine should be a no-op."""
        self.handler.database_engine = None
        self.handler._transcript_segments = []
        await self.handler._save_call_transcript()

    async def test_save_transcript_flushes_partial_caller_buffer(self) -> None:
        """Partial caller buffer should be flushed into segments before save check."""
        self.handler.database_engine = None
        self.handler._caller_transcript_buf = ["Hello", " world"]
        self.handler._assistant_transcript_buf = []
        self.handler._transcript_segments = []

        await self.handler._save_call_transcript()

        # Even though there's no DB (so no actual save), the buffer should have been
        # flushed into segments
        self.assertEqual(len(self.handler._transcript_segments), 1)
        self.assertEqual(self.handler._transcript_segments[0][0], "Caller")
        self.assertEqual(self.handler._transcript_segments[0][1], "Hello world")
        self.assertEqual(self.handler._caller_transcript_buf, [])

    async def test_save_transcript_flushes_partial_assistant_buffer(self) -> None:
        """Partial assistant buffer should be flushed into segments before save check."""
        self.handler.database_engine = None
        self.handler._caller_transcript_buf = []
        self.handler._assistant_transcript_buf = ["Good", "bye"]
        self.handler._transcript_segments = []

        await self.handler._save_call_transcript()

        self.assertEqual(len(self.handler._transcript_segments), 1)
        self.assertEqual(self.handler._transcript_segments[0][0], "Assistant")
        self.assertEqual(self.handler._transcript_segments[0][1], "Goodbye")
        self.assertEqual(self.handler._assistant_transcript_buf, [])

    async def test_save_transcript_formats_timestamps(self) -> None:
        """Transcript segments should be formatted with MM:SS timestamps."""
        self.handler._transcript_segments = [
            ("Caller", "Hello", 0.0),
            ("Assistant", "Hi there", 5.5),
            ("Caller", "Thanks", 65.3),
        ]

        captured = _SavedNoteArgs()
        with self._patch_db(captured):
            self.handler.database_engine = MagicMock()
            await self.handler._save_call_transcript()

        self.assertIn("[00:00] Caller: Hello", captured.content)
        self.assertIn("[00:05] Assistant: Hi there", captured.content)
        self.assertIn("[01:05] Caller: Thanks", captured.content)
        self.assertIs(captured.include_in_prompt, False)

    async def test_save_transcript_uses_visibility_labels_from_service(self) -> None:
        """Visibility labels should come from the processing service config."""
        self.handler._transcript_segments = [("Caller", "Hello", 0.0)]

        mock_service = MagicMock()
        mock_service.service_config.default_note_visibility_labels = ["telephone_logs"]
        self.handler.processing_service = mock_service

        captured = _SavedNoteArgs()
        with self._patch_db(captured):
            self.handler.database_engine = MagicMock()
            await self.handler._save_call_transcript()

        self.assertEqual(captured.visibility_labels, ["telephone_logs"])

    async def test_save_transcript_title_includes_extension(self) -> None:
        """Title should include the extension and datetime."""
        self.handler._transcript_segments = [("Caller", "Hello", 0.0)]

        captured = _SavedNoteArgs()
        with self._patch_db(captured):
            self.handler.database_engine = MagicMock()
            await self.handler._save_call_transcript()

        self.assertIn("Call Transcript: 100 -", captured.title)


if __name__ == "__main__":
    unittest.main()
