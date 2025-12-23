"""
Asterisk WebSocket Channel Driver Integration for Gemini Live.

This router implements the Asterisk WebSocket protocol to bridge Asterisk audio channels
with the Gemini Live API.

Protocol documentation: https://docs.asterisk.org/Configuration/Channel-Drivers/WebSocket/
"""

import asyncio
import base64
import contextlib
import json
import logging
import os
import secrets
import uuid
from datetime import datetime
from typing import Annotated, Any, cast
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from starlette.applications import Starlette
from starlette.websockets import WebSocketState

from family_assistant.web.audio_utils import StatefulResampler
from family_assistant.web.dependencies import get_live_audio_client
from family_assistant.web.models import GeminiLiveConfig
from family_assistant.web.voice_client import LiveAudioClient

logger = logging.getLogger(__name__)


def get_gemini_live_config_from_app(app: Starlette) -> GeminiLiveConfig:
    """Get the Gemini Live configuration from app state with telephone overrides."""
    return GeminiLiveConfig.from_app_state_with_telephone_overrides(app.state)


asterisk_live_router = APIRouter(tags=["Asterisk Live API"])

# Gemini Audio Constants
GEMINI_SAMPLE_RATE = 24000  # Gemini Live API output is 24kHz
GEMINI_CHANNELS = 1

# Check for dependencies (types only needed for config construction)
try:
    from google.genai.types import (
        AudioTranscriptionConfig,
        AutomaticActivityDetection,
        Content,
        EndSensitivity,
        LiveConnectConfig,
        Part,
        PrebuiltVoiceConfig,
        RealtimeInputConfig,
        SpeechConfig,
        StartSensitivity,
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
        gemini_live_config: GeminiLiveConfig,
        extension: str | None = None,
        conversation_id: str | None = None,
        system_instruction: str | None = None,
        tools: list[Any] | None = None,
    ) -> None:
        self.websocket = websocket
        self.client = client
        self.gemini_live_config = gemini_live_config
        self.extension = extension  # User identity (like Telegram chat_id)
        self.conversation_id = conversation_id or str(uuid.uuid4())  # For history
        self.system_instruction = system_instruction
        self.tools = tools
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

            # Build VAD configuration from gemini_live_config
            vad_config = self.gemini_live_config.vad

            # Map config sensitivity strings to SDK enums (None means use default)
            # Valid values: "DISABLED", "LOW", "DEFAULT", "HIGH"
            start_sensitivity: StartSensitivity | None = None
            if vad_config.start_of_speech_sensitivity != "DEFAULT":
                try:
                    start_sensitivity = StartSensitivity(
                        vad_config.start_of_speech_sensitivity
                    )
                except ValueError:
                    logger.warning(
                        f"Invalid start_of_speech_sensitivity value: "
                        f"'{vad_config.start_of_speech_sensitivity}', using default"
                    )

            end_sensitivity: EndSensitivity | None = None
            if vad_config.end_of_speech_sensitivity != "DEFAULT":
                try:
                    end_sensitivity = EndSensitivity(
                        vad_config.end_of_speech_sensitivity
                    )
                except ValueError:
                    logger.warning(
                        f"Invalid end_of_speech_sensitivity value: "
                        f"'{vad_config.end_of_speech_sensitivity}', using default"
                    )

            # Build AutomaticActivityDetection with all configured values
            # Only set disabled=True if automatic is False; otherwise leave as None
            is_disabled = True if not vad_config.automatic else None
            activity_detection = AutomaticActivityDetection(
                disabled=is_disabled,
                start_of_speech_sensitivity=start_sensitivity,
                end_of_speech_sensitivity=end_sensitivity,
                prefix_padding_ms=vad_config.prefix_padding_ms,
                silence_duration_ms=vad_config.silence_duration_ms,
            )

            # Only include realtime_input_config if we have non-default VAD settings
            has_custom_vad = (
                not vad_config.automatic
                or start_sensitivity is not None
                or end_sensitivity is not None
                or vad_config.prefix_padding_ms is not None
                or vad_config.silence_duration_ms is not None
            )
            realtime_input_config = None
            if has_custom_vad:
                logger.info(
                    f"Applying VAD settings: automatic={vad_config.automatic}, "
                    f"start_sensitivity={vad_config.start_of_speech_sensitivity}, "
                    f"end_sensitivity={vad_config.end_of_speech_sensitivity}, "
                    f"silence_duration_ms={vad_config.silence_duration_ms}"
                )
                realtime_input_config = RealtimeInputConfig(
                    automatic_activity_detection=activity_detection
                )

            config = LiveConnectConfig(
                response_modalities=cast("list[Any]", ["AUDIO"]),
                speech_config=SpeechConfig(
                    voice_config=VoiceConfig(
                        prebuilt_voice_config=PrebuiltVoiceConfig(
                            voice_name=self.gemini_live_config.voice.name
                        )
                    )
                ),
                system_instruction=Content(parts=[Part(text=self.system_instruction)])
                if self.system_instruction
                else None,
                tools=self.tools if self.tools else None,
                input_audio_transcription=AudioTranscriptionConfig(),
                output_audio_transcription=AudioTranscriptionConfig(),
                realtime_input_config=realtime_input_config,
            )

            # Use the injected client to connect
            # Note: Caller hears ringing during this connection setup (~5-10 seconds)
            async with self.client.connect(config=config) as session:
                self.gemini_session = session
                logger.info("Connected to Gemini Live")

                # Now that Gemini is ready, answer the call
                # This stops the ringing and connects audio
                await self._answer_call()

                # Start task to receive from Gemini and send to Asterisk
                self.receive_task = asyncio.create_task(self._receive_from_gemini())

                # Note: We don't trigger a greeting here - let natural voice
                # conversation flow. Caller speaks first, Gemini responds.

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
            # Asterisk -> Gemini: MUST resample to 16kHz (Gemini only accepts 16kHz input)
            # See: https://ai.google.dev/gemini-api/docs/live
            if self.sample_rate != 16000:
                self.asterisk_to_gemini_resampler = StatefulResampler(
                    self.sample_rate, 16000
                )

            # Gemini -> Asterisk: resample from 24kHz to target rate if needed
            if self.sample_rate != GEMINI_SAMPLE_RATE:
                self.gemini_to_asterisk_resampler = StatefulResampler(
                    GEMINI_SAMPLE_RATE, self.sample_rate
                )

        elif event_type == "HANGUP":
            await self.websocket.close()

    async def _answer_call(self) -> None:
        """Send ANSWER command to Asterisk to pick up the call.

        This should be called after Gemini is ready, so the caller stops hearing
        ringing and can immediately start conversing.
        """
        # Send as plain text - Asterisk accepts both plain text and JSON
        await self.websocket.send_text("ANSWER")
        logger.info("Sent ANSWER command to Asterisk")

    async def _trigger_greeting(self) -> None:
        """Send a trigger to Gemini to initiate the greeting.

        Gemini Live API is reactive by default - it waits for user input.
        We send a brief text message to prompt it to speak first.
        """
        if not self.gemini_session:
            return

        # Send a context message to trigger the greeting
        # The system instruction already tells Gemini to greet warmly
        await self.gemini_session.send_realtime_input(
            text="[The caller has just connected. Please greet them.]"
        )
        logger.info("Sent greeting trigger to Gemini")

    async def _handle_media_message(self, audio_data: bytes) -> None:
        """Handle media (audio) from Asterisk."""
        if not self.gemini_session:
            logger.warning("Received media but no Gemini session active")
            return

        logger.debug(
            f"Received {len(audio_data)} bytes from Asterisk ({self.sample_rate}Hz)"
        )

        # Resample if needed (Asterisk -> Gemini)
        # Gemini only accepts 16kHz input audio
        audio_to_send = audio_data

        if self.asterisk_to_gemini_resampler:
            audio_to_send = self.asterisk_to_gemini_resampler.resample(audio_data)
            logger.info(
                f"Resampled Asterisk audio: {len(audio_data)} -> {len(audio_to_send)} bytes"
            )

        # Gemini input MUST be 16kHz (see https://ai.google.dev/gemini-api/docs/live)
        mime_rate = 16000

        # Use send_realtime_input() for streaming audio to Gemini Live API
        # The audio dict must contain 'data' (bytes) and 'mime_type'
        # IMPORTANT: MIME type MUST include sample rate (e.g., audio/pcm;rate=16000)
        # Without the rate parameter, Gemini may misinterpret the audio causing distortion
        mime_type = f"audio/pcm;rate={mime_rate}"

        await self.gemini_session.send_realtime_input(
            audio={"data": audio_to_send, "mime_type": mime_type}
        )

    async def _receive_from_gemini(self) -> None:
        """Receive audio from Gemini and send to Asterisk."""
        if not self.gemini_session:
            return

        try:
            async for response in self.gemini_session.receive():
                # Log transcripts for debugging
                if response.server_content:
                    # Log input (user) transcription
                    if response.server_content.input_transcription:
                        text = response.server_content.input_transcription.text
                        if text:
                            logger.info(f"[TRANSCRIPT] User: {text}")

                    # Log output (model) transcription
                    if response.server_content.output_transcription:
                        text = response.server_content.output_transcription.text
                        if text:
                            logger.info(f"[TRANSCRIPT] Assistant: {text}")

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
                            raw_data = part.inline_data.data

                            # Gemini SDK returns raw audio bytes (PCM) in part.inline_data.data
                            audio_data = raw_data
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

    # Get Gemini Live configuration from app state
    gemini_live_config = get_gemini_live_config_from_app(websocket.app)

    # Get configuration from telephone profile if available
    system_instruction = None
    tools = None

    if GOOGLE_GENAI_TYPES_AVAILABLE:
        try:
            processing_services = getattr(
                websocket.app.state, "processing_services", {}
            )
            telephone_service = processing_services.get("telephone")

            if telephone_service:
                # Get system prompt
                prompts = telephone_service.service_config.prompts
                sys_prompt_template = prompts.get("system_prompt", "")
                if sys_prompt_template:
                    # Use configured timezone if available
                    timezone_str = telephone_service.service_config.timezone_str
                    tz = None
                    if timezone_str:
                        try:
                            tz = ZoneInfo(timezone_str)
                        except Exception:
                            logger.warning(
                                f"Invalid timezone '{timezone_str}' configured for telephone profile. Falling back to system time."
                            )

                    current_time = datetime.now(tz).strftime("%I:%M %p, %A, %B %d, %Y")
                    system_instruction = sys_prompt_template.replace(
                        "{current_time}", current_time
                    )

                # Get tools
                if telephone_service.tools_provider:
                    # Import here to avoid hard dependency on google-genai at module level
                    from family_assistant.llm.providers.google_genai_client import (  # noqa: PLC0415
                        convert_tools_to_genai_format,
                    )

                    raw_tools = (
                        await telephone_service.tools_provider.get_tool_definitions()
                    )
                    tools = convert_tools_to_genai_format(raw_tools)
                    logger.info(
                        f"Loaded {len(tools)} tools (Gemini format) for telephone profile"
                    )
            else:
                logger.warning(
                    "Telephone profile not found, using default configuration"
                )

        except Exception as e:
            logger.error(
                f"Error loading telephone profile configuration: {e}", exc_info=True
            )

    handler = AsteriskLiveHandler(
        websocket,
        client,
        gemini_live_config=gemini_live_config,
        extension=extension,
        conversation_id=conversation_id,
        system_instruction=system_instruction,
        tools=tools,
    )
    await handler.run()
