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
import secrets
import uuid
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from family_assistant.web.audio_utils import StatefulResampler
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
        LiveConnectConfig,
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


def get_asterisk_auth_config() -> tuple[str | None, set[str]]:
    """
    Get Asterisk authentication config from environment variables.

    Returns:
        Tuple of (secret_token, allowed_extensions).
        If secret_token is None, authentication is disabled.
        If allowed_extensions is empty, all extensions are allowed.
    """
    token = os.environ.get("ASTERISK_SECRET_TOKEN")
    allowed_str = os.environ.get("ASTERISK_ALLOWED_EXTENSIONS", "")
    allowed = {e.strip() for e in allowed_str.split(",") if e.strip()}
    return token, allowed


class AsteriskLiveHandler:
    """Handles the WebSocket connection from Asterisk and bridges it to Gemini Live."""

    def __init__(
        self,
        websocket: WebSocket,
        client: LiveAudioClient,
        extension: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        self.websocket = websocket
        self.client = client
        self.extension = extension  # User identity (like Telegram chat_id)
        self.conversation_id = conversation_id or str(uuid.uuid4())  # For history
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
        logger.info(
            f"Asterisk WebSocket accepted: conversation={self.conversation_id}, extension={self.extension}"
        )

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

        # Use send_realtime_input() for streaming audio to Gemini Live API
        # The audio dict must contain 'data' (bytes) and 'mime_type'
        await self.gemini_session.send_realtime_input(
            audio={"data": audio_to_send, "mime_type": "audio/pcm"}
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
    token: Annotated[str | None, Query()] = None,
    extension: Annotated[str | None, Query()] = None,
    channel_id: Annotated[str | None, Query()] = None,
) -> None:
    """
    WebSocket endpoint for Asterisk Live Audio.

    Authentication:
        - Set ASTERISK_SECRET_TOKEN env var to require token authentication
        - Pass token as query parameter: ?token=<secret>
        - Set ASTERISK_ALLOWED_EXTENSIONS to restrict by extension (comma-separated)

    Query Parameters:
        - token: Authentication token (required if ASTERISK_SECRET_TOKEN is set)
        - extension: Caller's extension number (user identity)
        - channel_id: Asterisk channel ID (used as conversation ID for history)

    Example Asterisk Dialplan:
        Dial(WebSocket/host:8000/api/asterisk/live?token=${TOKEN}&extension=${CALLERID(num)}&channel_id=${CHANNEL})
    """
    secret_token, allowed_extensions = get_asterisk_auth_config()

    # Layer 1: Server authentication
    if secret_token and (not token or not secrets.compare_digest(token, secret_token)):
        logger.warning("Asterisk connection rejected: invalid or missing token")
        await websocket.close(code=1008, reason="Invalid token")
        return

    # Layer 2: Extension authorization (if allow-list configured)
    if allowed_extensions and extension not in allowed_extensions:
        logger.warning(
            f"Asterisk connection rejected: extension '{extension}' not in allowed list"
        )
        await websocket.close(code=1008, reason="Extension not authorized")
        return

    # Use channel_id as conversation_id for history, fall back to UUID
    conversation_id = channel_id or str(uuid.uuid4())

    logger.info(
        f"Asterisk connection authorized: extension={extension}, channel={conversation_id}"
    )

    handler = AsteriskLiveHandler(
        websocket, client, extension=extension, conversation_id=conversation_id
    )
    await handler.run()
