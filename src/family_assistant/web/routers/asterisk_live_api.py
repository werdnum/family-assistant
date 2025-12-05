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
import os
import uuid
from typing import Any, cast

import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

logger = logging.getLogger(__name__)

asterisk_live_router = APIRouter(tags=["Asterisk Live API"])

# Gemini Constants
GEMINI_MODEL = "gemini-2.5-flash-native-audio-preview-09-2025"
GEMINI_SAMPLE_RATE = 24000  # Gemini Live API output is 24kHz
GEMINI_CHANNELS = 1

# Check for dependencies
try:
    from google import genai
    from google.genai.types import (
        Blob,
        Content,
        LiveConnectConfig,
        Part,
        PrebuiltVoiceConfig,
        SpeechConfig,
        VoiceConfig,
    )

    GOOGLE_GENAI_AVAILABLE = True
except ImportError:
    GOOGLE_GENAI_AVAILABLE = False
    logger.warning("google-genai package not found. Asterisk Live API will not work.")


class AsteriskLiveHandler:
    """Handles the WebSocket connection from Asterisk and bridges it to Gemini Live."""

    def __init__(self, websocket: WebSocket, api_key: str) -> None:
        self.websocket = websocket
        self.api_key = api_key
        self.connection_id = str(uuid.uuid4())
        self.sample_rate = 8000  # Default to 8kHz (slin)
        self.optimal_frame_size = 320  # Default for 8kHz 20ms
        self.audio_buffer = bytearray()
        self.gemini_session: Any | None = None
        self.receive_task: asyncio.Task[None] | None = None
        self.client: Any | None = None
        self.format: str | None = None

    async def run(self) -> None:
        """Main loop for the handler."""
        await self.websocket.accept()
        logger.info(f"Accepted Asterisk WebSocket connection {self.connection_id}")

        if not GOOGLE_GENAI_AVAILABLE:
            logger.error("google-genai not available")
            await self.websocket.close(code=1011)
            return

        try:
            self.client = genai.Client(
                api_key=self.api_key, http_options={"api_version": "v1alpha"}
            )

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

            async with self.client.aio.live.connect(
                model=GEMINI_MODEL, config=config
            ) as session:
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
            # Example: MEDIA_START connection_id:xyz ...
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

        elif event_type == "HANGUP":
            await self.websocket.close()

    async def _handle_media_message(self, audio_data: bytes) -> None:
        """Handle media (audio) from Asterisk."""
        if not self.gemini_session:
            return

        # Resample if needed (Asterisk -> Gemini)
        # Gemini supports 16kHz and 24kHz input.
        # If 8kHz, we should upsample to 16kHz or 24kHz.
        # If 16kHz or 24kHz, send as is.

        audio_to_send = audio_data

        if self.sample_rate == 8000:
            # Upsample to 16kHz for better compatibility
            audio_to_send = self._resample_audio(audio_data, 8000, 16000)
        elif self.sample_rate not in {16000, 24000}:
            # Try to resample to 24kHz as generic fallback
            audio_to_send = self._resample_audio(audio_data, self.sample_rate, 24000)

        # Send to Gemini
        mime_rate = 16000 if self.sample_rate == 8000 else self.sample_rate
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
                            if self.sample_rate != GEMINI_SAMPLE_RATE:
                                target_audio = self._resample_audio(
                                    audio_data, GEMINI_SAMPLE_RATE, self.sample_rate
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

    def _resample_audio(self, audio_data: bytes, src_rate: int, dst_rate: int) -> bytes:
        """Resample PCM audio using numpy linear interpolation."""
        if src_rate == dst_rate or not audio_data:
            return audio_data

        try:
            # Convert bytes to numpy array (int16)
            audio_np = np.frombuffer(audio_data, dtype=np.int16)

            if len(audio_np) == 0:
                return b""

            # Create time points and calculate new length based on rates
            new_length = int(len(audio_np) * dst_rate / src_rate)

            x_old = np.arange(len(audio_np))
            x_new = np.linspace(0, len(audio_np) - 1, new_length)

            # Interpolate
            new_audio_np = np.interp(x_new, x_old, audio_np).astype(np.int16)

            return new_audio_np.tobytes()
        except Exception as e:
            logger.error(f"Resampling error: {e}")
            return audio_data


@asterisk_live_router.websocket("/asterisk/live")
async def asterisk_live_endpoint(websocket: WebSocket) -> None:
    """
    WebSocket endpoint for Asterisk Live Audio.
    Connect to this using Dial(WebSocket/host/path/asterisk/live).
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.error("GEMINI_API_KEY not set")
        await websocket.close(code=1008, reason="API Key missing")
        return

    handler = AsteriskLiveHandler(websocket, api_key)
    await handler.run()
