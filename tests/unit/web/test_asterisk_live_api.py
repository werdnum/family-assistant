import asyncio
import unittest
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

from google.genai.types import Blob, Part
from starlette.websockets import WebSocketState

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


if __name__ == "__main__":
    unittest.main()
