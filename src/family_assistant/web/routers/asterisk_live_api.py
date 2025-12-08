"""
Asterisk WebSocket Channel Driver Integration for Gemini Live.

This router implements the Asterisk WebSocket protocol to bridge Asterisk audio channels
with the Gemini Live API.

Protocol documentation: https://docs.asterisk.org/Configuration/Channel-Drivers/WebSocket/
"""

import asyncio
import contextlib
import json
import logging
import uuid
from typing import Annotated, Any, cast

import numpy as np
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from family_assistant.web.dependencies import get_live_audio_client
from family_assistant.web.voice_client import LiveAudioClient

logger = logging.getLogger(__name__)

asterisk_live_router = APIRouter(tags=["Asterisk Live API"])

# Gemini Audio Constants
GEMINI_SAMPLE_RATE = 24000  # Gemini Live API output is 24kHz
GEMINI_CHANNELS = 1

# Check for dependencies (types only needed for config construction)
try:
    from google.genai.types import (
        Blob,
        Content,
        LiveConnectConfig,
        Part,
        PrebuiltVoiceConfig,
        SpeechConfig,
        VoiceConfig,
    )

    GOOGLE_GENAI_TYPES_AVAILABLE = True
except ImportError:
    GOOGLE_GENAI_TYPES_AVAILABLE = False
    logger.warning(
        "google-genai types not found. Asterisk Live API will not work fully."
    )


class StatefulResampler:
    """
    Stateful audio resampler that maintains continuity across chunks.
    Uses libsoxr for high-quality, low-latency resampling suitable for real-time audio.
    """

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self.resampler: Any | None = None

        # Try to initialize soxr resampler
        try:
            import soxr  # noqa: PLC0415

            # Create a resampler instance that maintains state
            # quality='VHQ' provides Very High Quality suitable for telephony
            # For even lower latency, could use 'HQ' (High Quality)
            self.resampler = soxr.ResampleStream(
                src_rate, dst_rate, num_channels=1, dtype="int16", quality="VHQ"
            )
            logger.debug(
                f"Initialized soxr resampler: {src_rate}Hz -> {dst_rate}Hz (VHQ)"
            )
        except ImportError:
            logger.warning(
                "soxr not available, will fall back to linear interpolation (lower quality)"
            )
        except Exception as e:
            logger.error(f"Failed to initialize soxr resampler: {e}", exc_info=True)

    def resample(self, audio_data: bytes) -> bytes:
        """Resample audio data maintaining filter state across calls."""
        if self.src_rate == self.dst_rate or not audio_data:
            return audio_data

        if self.resampler:
            try:
                # Convert bytes to numpy array
                audio_np = np.frombuffer(audio_data, dtype=np.int16)
                if len(audio_np) == 0:
                    return b""

                # Resample using stateful resampler
                # The resampler maintains internal state for continuity
                resampled = self.resampler.resample_chunk(audio_np)

                return resampled.astype(np.int16).tobytes()
            except Exception as e:
                logger.error(f"soxr resampling error: {e}", exc_info=True)
                # Fall through to linear interpolation

        # Fallback to linear interpolation
        return self._resample_linear(audio_data)

    def _resample_linear(self, audio_data: bytes) -> bytes:
        """Fallback linear interpolation resampling (lower quality)."""
        if not audio_data:
            return audio_data

        try:
            audio_np = np.frombuffer(audio_data, dtype=np.int16)
            input_len = len(audio_np)

            if input_len == 0:
                return b""

            # Calculate new length based on rates
            new_length = int(input_len * self.dst_rate / self.src_rate)

            # Calculate sample positions based on sample rate ratio to preserve pitch
            # For each output sample i, input position = i * src_rate / dst_rate
            x_old = np.arange(input_len)
            x_new = np.arange(new_length) * self.src_rate / self.dst_rate
            new_audio_np = np.interp(x_new, x_old, audio_np).astype(np.int16)

            return new_audio_np.tobytes()
        except Exception as e:
            logger.error(f"Linear resampling error: {e}", exc_info=True)
            return audio_data


class AsteriskLiveHandler:
    """Handles the WebSocket connection from Asterisk and bridges it to Gemini Live."""

    def __init__(self, websocket: WebSocket, client: LiveAudioClient) -> None:
        self.websocket = websocket
        self.client = client
        self.connection_id = str(uuid.uuid4())
        self.sample_rate = 8000  # Default to 8kHz (slin)
        self.optimal_frame_size = 320  # Default for 8kHz 20ms
        self.audio_buffer = bytearray()
        self.gemini_session: Any | None = None
        self.receive_task: asyncio.Task[None] | None = None
        self.format: str | None = None
        # Stateful resamplers for bidirectional audio
        self.asterisk_to_gemini_resampler: StatefulResampler | None = None
        self.gemini_to_asterisk_resampler: StatefulResampler | None = None

    async def run(self) -> None:
        """Main loop for the handler."""
        await self.websocket.accept()
        logger.info(f"Accepted Asterisk WebSocket connection {self.connection_id}")

        if not GOOGLE_GENAI_TYPES_AVAILABLE:
            logger.error("google-genai types not available")
            await self.websocket.close(code=1011)
            return

        try:
            # Wait for initial configuration (MEDIA_START) from Asterisk
            # This ensures we know the sample rate before establishing the Gemini session
            while not self.format:
                message = await self.websocket.receive()

                if message["type"] == "websocket.disconnect":
                    logger.info("Asterisk disconnected before configuration")
                    return

                if "text" in message:
                    await self._handle_control_message(message["text"])

                # Ignore media messages until configured (or log warning)
                elif "bytes" in message:
                    logger.warning("Received media before configuration, ignoring")

            logger.info(f"Configuration received. Format: {self.format}")

            config = LiveConnectConfig(
                response_modalities=cast("list[Any]", ["AUDIO"]),
                speech_config=SpeechConfig(
                    voice_config=VoiceConfig(
                        prebuilt_voice_config=PrebuiltVoiceConfig(
                            voice_name="Puck"  # Default voice
                        )
                    )
                ),
            )

            # Use the injected client to connect
            async with self.client.connect(config=config) as session:
                self.gemini_session = session
                logger.info("Connected to Gemini Live")

                # Start task to receive from Gemini and send to Asterisk
                self.receive_task = asyncio.create_task(self._receive_from_gemini())

                try:
                    while True:
                        # Receive message from Asterisk
                        message = await self.websocket.receive()

                        if message["type"] == "websocket.disconnect":
                            logger.info("Asterisk disconnected")
                            break

                        if "text" in message:
                            await self._handle_control_message(message["text"])
                        elif "bytes" in message:
                            await self._handle_media_message(message["bytes"])

                except WebSocketDisconnect:
                    logger.info("WebSocket disconnected")
                except Exception as e:
                    logger.error(f"Error in Asterisk loop: {e}", exc_info=True)
                finally:
                    if self.receive_task:
                        self.receive_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await self.receive_task

        except Exception as e:
            logger.error(f"Error in AsteriskLiveHandler: {e}", exc_info=True)
            if self.websocket.client_state == WebSocketState.CONNECTED:
                await self.websocket.close(code=1011)

    async def _handle_control_message(self, text: str) -> None:
        """Handle control messages from Asterisk (JSON or Plain Text)."""
        data = {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Handle plain text format
            parts = text.split()
            if not parts:
                return
            data["event"] = parts[0]  # Assume event name is first
            for part in parts[1:]:
                if ":" in part:
                    key, value = part.split(":", 1)
                    data[key] = value

        event_type = data.get("event") or data.get("command")
        logger.debug(f"Received Asterisk control event: {event_type} - {data}")

        if event_type == "MEDIA_START":
            # Parse format and optimal_frame_size
            self.format = data.get("format")
            if "optimal_frame_size" in data:
                self.optimal_frame_size = int(data["optimal_frame_size"])

            # Determine sample rate from format
            if self.format == "slin16":
                self.sample_rate = 16000
            elif self.format == "slin24":
                self.sample_rate = 24000
            elif self.format == "slin":
                self.sample_rate = 8000
            elif self.format in {"ulaw", "alaw"}:
                self.sample_rate = 8000
                logger.warning(
                    f"Codec {self.format} requested. Will attempt to treat as 8kHz PCM but requires transcoding which is not fully implemented for compressed codecs."
                )
            else:
                logger.warning(
                    f"Unknown format {self.format}, defaulting to {self.sample_rate}"
                )

            logger.info(
                f"Configured: format={self.format}, rate={self.sample_rate}, frame_size={self.optimal_frame_size}"
            )

            # Reset resamplers to prevent stale configuration on re-configuration
            self.asterisk_to_gemini_resampler = None
            self.gemini_to_asterisk_resampler = None

            # Initialize resamplers based on the sample rate
            # Asterisk -> Gemini: resample to 16kHz for 8kHz input, otherwise use native rate
            if self.sample_rate == 8000:
                self.asterisk_to_gemini_resampler = StatefulResampler(8000, 16000)
            elif self.sample_rate not in {16000, 24000}:
                # For other rates, resample to 24kHz
                self.asterisk_to_gemini_resampler = StatefulResampler(
                    self.sample_rate, 24000
                )

            # Gemini -> Asterisk: resample from 24kHz to target rate if needed
            if self.sample_rate != GEMINI_SAMPLE_RATE:
                self.gemini_to_asterisk_resampler = StatefulResampler(
                    GEMINI_SAMPLE_RATE, self.sample_rate
                )

        elif event_type == "HANGUP":
            await self.websocket.close()

    async def _handle_media_message(self, audio_data: bytes) -> None:
        """Handle media (audio) from Asterisk."""
        if not self.gemini_session:
            return

        # Resample if needed (Asterisk -> Gemini)
        audio_to_send = audio_data
        mime_rate = self.sample_rate

        if self.asterisk_to_gemini_resampler:
            audio_to_send = self.asterisk_to_gemini_resampler.resample(audio_data)
            mime_rate = self.asterisk_to_gemini_resampler.dst_rate

        # Ensure mime_rate is valid for Gemini
        if mime_rate not in {16000, 24000}:
            mime_rate = 24000  # Fallback

        await self.gemini_session.send(
            input=Content(
                parts=[
                    Part(
                        inline_data=Blob(
                            data=audio_to_send, mime_type=f"audio/pcm;rate={mime_rate}"
                        )
                    )
                ]
            ),
            end_of_turn=False,
        )

    async def _receive_from_gemini(self) -> None:
        """Receive audio from Gemini and send to Asterisk."""
        if not self.gemini_session:
            return

        try:
            async for response in self.gemini_session.receive():
                # Extract audio data
                server_content = response.server_content
                if server_content and server_content.model_turn:
                    for part in server_content.model_turn.parts:
                        if (
                            part.inline_data
                            and part.inline_data.mime_type
                            and part.inline_data.mime_type.startswith("audio")
                            and part.inline_data.data
                        ):
                            audio_data = part.inline_data.data
                            # Gemini output is 24kHz PCM

                            # Resample if needed (Gemini -> Asterisk)
                            target_audio = audio_data
                            if self.gemini_to_asterisk_resampler:
                                target_audio = (
                                    self.gemini_to_asterisk_resampler.resample(
                                        audio_data
                                    )
                                )

                            # Buffer and send
                            self.audio_buffer.extend(target_audio)

                            while len(self.audio_buffer) >= self.optimal_frame_size:
                                chunk = self.audio_buffer[: self.optimal_frame_size]
                                del self.audio_buffer[: self.optimal_frame_size]
                                await self.websocket.send_bytes(bytes(chunk))

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error receiving from Gemini: {e}", exc_info=True)


@asterisk_live_router.websocket("/asterisk/live")
async def asterisk_live_endpoint(
    websocket: WebSocket,
    client: Annotated[LiveAudioClient, Depends(get_live_audio_client)],
) -> None:
    """
    WebSocket endpoint for Asterisk Live Audio.
    Connect to this using Dial(WebSocket/host/path/asterisk/live).
    """
    handler = AsteriskLiveHandler(websocket, client)
    await handler.run()
