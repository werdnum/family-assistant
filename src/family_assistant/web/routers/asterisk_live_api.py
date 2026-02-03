"""
Asterisk WebSocket Channel Driver Integration for Gemini Live.

This router implements the Asterisk WebSocket protocol to bridge Asterisk audio channels
with the Gemini Live API.

Protocol documentation: https://docs.asterisk.org/Configuration/Channel-Drivers/WebSocket/
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import pathlib
import secrets
import time
import uuid
from array import array
from datetime import datetime
from typing import IO, TYPE_CHECKING, Annotated, Protocol, TypeAlias, cast
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from google.genai.types import (
    AudioTranscriptionConfig,
    AutomaticActivityDetection,
    Blob,
    Content,
    EndSensitivity,
    FunctionCall,
    FunctionResponse,
    LiveConnectConfig,
    LiveServerMessage,
    LiveServerToolCall,
    Modality,
    Part,
    PrebuiltVoiceConfig,
    ProactivityConfig,
    RealtimeInputConfig,
    SpeechConfig,
    StartSensitivity,
    ToolListUnion,
    VoiceConfig,
)
from starlette.websockets import WebSocketState

from family_assistant.storage.context import get_db_context
from family_assistant.tools.types import ToolExecutionContext, ToolResult
from family_assistant.web.audio_utils import StatefulResampler
from family_assistant.web.dependencies import get_live_audio_client
from family_assistant.web.models import GeminiLiveConfig
from family_assistant.web.voice_client import (  # noqa: TC001 - FastAPI resolves this at runtime for Depends
    LiveAudioClient,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from google.genai.live import AsyncSession
    from starlette.applications import Starlette

    from family_assistant.processing import ProcessingService

logger = logging.getLogger(__name__)

_DEBUG_ENV = "ASTERISK_LIVE_DEBUG"
_DUMP_DIR_ENV = "ASTERISK_LIVE_DUMP_DIR"
_DEFAULT_DUMP_DIR = "/var/log/family-assistant/asterisk-live"
_DEFAULT_DUCK_MS = 400
_DEFAULT_DUCK_GAIN = 0.1
_PACE_LOG_EVERY = 50
_MIN_SEND_FRAME_MS = 40


def _env_flag(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name, "")
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, *, default: float) -> float:
    value = os.environ.get(name, "")
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _sanitize_filename(value: str) -> str:
    if not value:
        return "unknown"
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)


def get_gemini_live_config_from_app(app: Starlette) -> GeminiLiveConfig:
    """Get the Gemini Live configuration from app state with telephone overrides."""
    return GeminiLiveConfig.from_app_state_with_telephone_overrides(app.state)


asterisk_live_router = APIRouter(tags=["Asterisk Live API"])

# Gemini Audio Constants
GEMINI_SAMPLE_RATE = 24000  # Gemini Live API output is 24kHz
GEMINI_CHANNELS = 1

JsonPrimitive: TypeAlias = str | int | float | bool | None  # noqa: UP040
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]  # noqa: UP040


class SupportsRepr(Protocol):
    def __repr__(self) -> str: ...


SerializableValue: TypeAlias = (  # noqa: UP040
    JsonValue
    | bytes
    | bytearray
    | list["SerializableValue"]
    | dict[str, "SerializableValue"]
    | tuple["SerializableValue", ...]
    | SupportsRepr
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
        processing_service: ProcessingService | None = None,
        extension: str | None = None,
        conversation_id: str | None = None,
        system_instruction: str | None = None,
        tools: ToolListUnion | None = None,
    ) -> None:
        self.websocket = websocket
        self.client = client
        self.gemini_live_config = gemini_live_config
        self.processing_service = processing_service
        self.extension = extension  # User identity (like Telegram chat_id)
        self.conversation_id = conversation_id or str(uuid.uuid4())  # For history
        self.system_instruction = system_instruction
        self.tools = tools
        self.sample_rate = 8000  # Default to 8kHz (slin)
        self.optimal_frame_size = 320  # Default for 8kHz 20ms
        self.send_frame_size = self.optimal_frame_size
        self.send_frame_duration_ms = 20.0
        self.audio_buffer = bytearray()
        self.gemini_session: AsyncSession | None = None
        self.receive_task: asyncio.Task[None] | None = None
        self.format: str | None = None
        self.media_send_allowed: asyncio.Event = asyncio.Event()
        self.debug_enabled = _env_flag(_DEBUG_ENV, default=False)
        self.assistant_duck_ms = int(
            os.environ.get("ASTERISK_LIVE_DUCK_MS", _DEFAULT_DUCK_MS)
        )
        self.assistant_duck_gain = _env_float(
            "ASTERISK_LIVE_DUCK_GAIN", default=_DEFAULT_DUCK_GAIN
        )
        self.assistant_speaking_until = 0.0
        dump_dir_env = os.environ.get(_DUMP_DIR_ENV, "").strip()
        if dump_dir_env:
            self.dump_dir = dump_dir_env
        elif self.debug_enabled:
            self.dump_dir = _DEFAULT_DUMP_DIR
        else:
            self.dump_dir = ""
        self._dump_base_path: pathlib.Path | None = None
        self._dump_files: dict[str, IO[bytes]] = {}
        self._trace_file: IO[str] | None = None
        self._dump_lock = asyncio.Lock()
        self._packet_seq = 0
        self._pacing_samples = 0
        self._pacing_interval_sum_ms = 0.0
        self._pacing_interval_max_ms = 0.0
        self._pacing_last_send_ts = 0.0
        self._pacing_underflows = 0
        # Stateful resamplers for bidirectional audio
        self.asterisk_to_gemini_resampler: StatefulResampler | None = None
        self.gemini_to_asterisk_resampler: StatefulResampler | None = None
        # Allow media to flow by default until we receive an XOFF from Asterisk
        self.media_send_allowed.set()
        self._debug_log(
            f"Asterisk live debug: enabled={self.debug_enabled} dump_dir={self.dump_dir}"
        )
        self._debug_log(
            f"Asterisk live ducking: {self.assistant_duck_ms}ms gain={self.assistant_duck_gain}"
        )
        app = getattr(self.websocket, "app", None)
        app_state = getattr(app, "state", None)
        self.database_engine = getattr(app_state, "database_engine", None)

    def _debug_log(self, message: str) -> None:
        if self.debug_enabled:
            logger.info(message)

    def _encode_bytes(self, data: bytes | bytearray) -> str:
        return base64.b64encode(bytes(data)).decode("ascii")

    def _apply_ducking(self, audio_data: bytes) -> bytes:
        if self.assistant_duck_gain >= 0.999:
            return audio_data
        if len(audio_data) % 2 != 0:
            audio_data = audio_data[:-1]
        samples = array("h")
        samples.frombytes(audio_data)
        for idx, sample in enumerate(samples):
            scaled = int(sample * self.assistant_duck_gain)
            if scaled > 32767:
                scaled = 32767
            elif scaled < -32768:
                scaled = -32768
            samples[idx] = scaled
        return samples.tobytes()

    def _safe_serialize(
        self, value: SerializableValue, *, depth: int = 4, seen: set[int] | None = None
    ) -> JsonValue:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value

        if isinstance(value, (bytes, bytearray)):
            return {"b64": self._encode_bytes(value), "bytes": len(value)}

        if depth <= 0:
            return repr(value)

        if seen is None:
            seen = set()
        obj_id = id(value)
        if obj_id in seen:
            return "<cycle>"
        seen.add(obj_id)

        if isinstance(value, dict):
            return {
                str(key): self._safe_serialize(val, depth=depth - 1, seen=seen)
                for key, val in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            return [
                self._safe_serialize(item, depth=depth - 1, seen=seen) for item in value
            ]

        for attr in ("model_dump", "to_dict"):
            func = getattr(value, attr, None)
            if callable(func):
                try:
                    return self._safe_serialize(func(), depth=depth - 1, seen=seen)
                except Exception:
                    pass

        if hasattr(value, "__dict__"):
            try:
                return self._safe_serialize(value.__dict__, depth=depth - 1, seen=seen)
            except Exception:
                pass

        return repr(value)

    def _ensure_dump_files(self) -> None:
        if not self.dump_dir or self._dump_base_path:
            return

        base_dir = pathlib.Path(self.dump_dir)
        base_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
        safe_conv = _sanitize_filename(self.conversation_id)
        safe_ext = _sanitize_filename(self.extension or "unknown")
        session_dir = base_dir / f"{timestamp}_{safe_ext}_{safe_conv}"
        session_dir.mkdir(parents=True, exist_ok=True)

        self._dump_base_path = session_dir
        self._dump_files = {
            "asterisk_in": (session_dir / "asterisk_in.pcm").open("ab"),
            "gemini_in": (session_dir / "gemini_in.pcm").open("ab"),
            "gemini_out": (session_dir / "gemini_out.pcm").open("ab"),
            "asterisk_out": (session_dir / "asterisk_out.pcm").open("ab"),
        }
        self._trace_file = (session_dir / "packet_trace.jsonl").open(
            "a", encoding="utf-8"
        )

        self._debug_log(f"Asterisk live dump enabled: {session_dir}")

    async def _close_dump_files(self) -> None:
        if not self._dump_base_path:
            return

        async with self._dump_lock:
            for handle in self._dump_files.values():
                with contextlib.suppress(Exception):
                    handle.flush()
                    handle.close()
            self._dump_files.clear()

            if self._trace_file:
                with contextlib.suppress(Exception):
                    self._trace_file.flush()
                    self._trace_file.close()
            self._trace_file = None

    async def _trace_event(
        self, kind: str, direction: str | None, **fields: JsonValue
    ) -> None:
        if not self.dump_dir:
            return

        self._ensure_dump_files()
        if not self._trace_file:
            return

        self._packet_seq += 1
        record: dict[str, JsonValue] = {
            "ts": datetime.now(ZoneInfo("UTC")).isoformat(),
            "mono_ts": time.monotonic(),
            "seq": self._packet_seq,
            "kind": kind,
            "direction": direction,
            "conversation_id": self.conversation_id,
            "extension": self.extension,
            "format": self.format,
            "sample_rate": self.sample_rate,
            "frame_size": self.optimal_frame_size,
        }
        record.update(fields)

        line = json.dumps(record, separators=(",", ":"), default=str)
        async with self._dump_lock:
            self._trace_file.write(f"{line}\n")
            self._trace_file.flush()

    async def _dump_audio(self, stream: str, data: bytes) -> None:
        if not self.dump_dir or not data:
            return

        self._ensure_dump_files()
        handle = self._dump_files.get(stream)
        if not handle:
            return

        async with self._dump_lock:
            handle.write(data)
            handle.flush()

    async def run(self) -> None:
        """Main loop for the handler."""
        await self.websocket.accept()
        logger.info(
            f"Asterisk WebSocket accepted: conversation={self.conversation_id}, extension={self.extension}"
        )
        await self._trace_event(
            "lifecycle",
            "asterisk->fa",
            event="websocket_accept",
        )

        try:
            # Wait for initial configuration (MEDIA_START) from Asterisk
            # This ensures we know the sample rate before establishing the Gemini session
            while not self.format:
                message = await self.websocket.receive()

                if message["type"] == "websocket.disconnect":
                    logger.info("Asterisk disconnected before configuration")
                    await self._trace_event(
                        "lifecycle",
                        "asterisk->fa",
                        event="websocket_disconnect",
                        payload=self._safe_serialize(message),
                    )
                    return

                if "text" in message:
                    await self._trace_event(
                        "control",
                        "asterisk->fa",
                        raw=message["text"],
                        message_type=message.get("type"),
                    )
                    await self._handle_control_message(message["text"])

                # Ignore media messages until configured (or log warning)
                elif "bytes" in message:
                    await self._trace_event(
                        "media",
                        "asterisk->fa",
                        bytes=len(message["bytes"]),
                        b64=self._encode_bytes(message["bytes"]),
                        note="media_before_configuration",
                        message_type=message.get("type"),
                    )
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

            # Build proactivity configuration
            # See: https://ai.google.dev/gemini-api/docs/live-guide#proactive-audio
            proactivity = None
            if self.gemini_live_config.proactivity.enabled:
                proactivity = ProactivityConfig(
                    proactive_audio=self.gemini_live_config.proactivity.proactive_audio
                )

            config = LiveConnectConfig(
                response_modalities=[Modality.AUDIO],
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
                proactivity=proactivity,
            )

            # Use the injected client to connect
            # Note: Caller hears ringing during this connection setup (~5-10 seconds)
            async with self.client.connect(config=config) as session:
                self.gemini_session = session
                logger.info("Connected to Gemini Live")

                # Now that Gemini is ready, answer the call
                # This stops the ringing and connects audio
                await self._answer_call()
                await self._trace_event(
                    "lifecycle",
                    "fa->asterisk",
                    event="gemini_connected_answered",
                )

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
                            await self._trace_event(
                                "lifecycle",
                                "asterisk->fa",
                                event="websocket_disconnect",
                                payload=self._safe_serialize(message),
                            )
                            break

                        if "text" in message:
                            await self._trace_event(
                                "control",
                                "asterisk->fa",
                                raw=message["text"],
                                message_type=message.get("type"),
                            )
                            await self._handle_control_message(message["text"])
                        elif "bytes" in message:
                            await self._handle_media_message(message["bytes"])
                        else:
                            await self._trace_event(
                                "websocket_message",
                                "asterisk->fa",
                                payload=self._safe_serialize(message),
                            )

                except WebSocketDisconnect:
                    logger.info("WebSocket disconnected")
                    await self._trace_event(
                        "lifecycle",
                        "asterisk->fa",
                        event="websocket_disconnect",
                    )
                except Exception as e:
                    logger.error(f"Error in Asterisk loop: {e}", exc_info=True)
                    await self._trace_event(
                        "error",
                        "fa",
                        error=str(e),
                    )
                finally:
                    if self.receive_task:
                        self.receive_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await self.receive_task

        except Exception as e:
            logger.error(f"Error in AsteriskLiveHandler: {e}", exc_info=True)
            await self._trace_event(
                "error",
                "fa",
                error=str(e),
            )
            if self.websocket.client_state == WebSocketState.CONNECTED:
                await self.websocket.close(code=1011)
        finally:
            await self._close_dump_files()

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
        self._debug_log(f"Asterisk control event: {event_type} - {data}")

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
            min_frame_bytes = int(self.sample_rate * 2 * (_MIN_SEND_FRAME_MS / 1000.0))
            self.send_frame_size = max(self.optimal_frame_size, min_frame_bytes)
            self.send_frame_duration_ms = (
                self.send_frame_size / (self.sample_rate * 2)
            ) * 1000.0
            logger.info(
                "Asterisk send pacing: "
                f"send_frame_size={self.send_frame_size} "
                f"frame_duration_ms={self.send_frame_duration_ms:.2f}"
            )
            self._pacing_samples = 0
            self._pacing_interval_sum_ms = 0.0
            self._pacing_interval_max_ms = 0.0
            self._pacing_last_send_ts = 0.0
            self._pacing_underflows = 0
            await self._trace_event(
                "control",
                "asterisk->fa",
                event=event_type,
                parsed=self._safe_serialize(data),
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
            await self._trace_event(
                "control",
                "asterisk->fa",
                event=event_type,
                parsed=self._safe_serialize(data),
            )
            await self.websocket.close()

        elif event_type == "MEDIA_XOFF":
            logger.info("Received MEDIA_XOFF; pausing media sends to Asterisk")
            await self._trace_event(
                "control",
                "asterisk->fa",
                event=event_type,
                parsed=self._safe_serialize(data),
            )
            self.media_send_allowed.clear()

        elif event_type == "MEDIA_XON":
            if not self.media_send_allowed.is_set():
                logger.info("Received MEDIA_XON; resuming media sends to Asterisk")
            await self._trace_event(
                "control",
                "asterisk->fa",
                event=event_type,
                parsed=self._safe_serialize(data),
            )
            self.media_send_allowed.set()
        else:
            await self._trace_event(
                "control",
                "asterisk->fa",
                event=event_type,
                parsed=self._safe_serialize(data),
            )

    async def _answer_call(self) -> None:
        """Send ANSWER command to Asterisk to pick up the call.

        This should be called after Gemini is ready, so the caller stops hearing
        ringing and can immediately start conversing.
        """
        # Send as plain text - Asterisk accepts both plain text and JSON
        await self.websocket.send_text("ANSWER")
        await self._trace_event(
            "control",
            "fa->asterisk",
            event="ANSWER",
            payload="ANSWER",
        )
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
        await self._trace_event(
            "gemini_send",
            "fa->gemini",
            payload={"text": "[The caller has just connected. Please greet them.]"},
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
        if (
            self.assistant_duck_ms > 0
            and self.assistant_duck_gain < 0.999
            and time.monotonic() < self.assistant_speaking_until
        ):
            audio_data = self._apply_ducking(audio_data)
            await self._trace_event(
                "duck",
                "fa",
                event="duck_user_audio",
                duck_ms=self.assistant_duck_ms,
                gain=self.assistant_duck_gain,
                bytes=len(audio_data),
            )
        self._debug_log(
            f"Asterisk media in: {len(audio_data)} bytes, format={self.format}, rate={self.sample_rate}"
        )
        await self._trace_event(
            "media",
            "asterisk->fa",
            bytes=len(audio_data),
            b64=self._encode_bytes(audio_data),
            hex_prefix=audio_data[:16].hex(),
            buffer_len=len(self.audio_buffer),
        )
        await self._dump_audio("asterisk_in", audio_data)

        # Resample if needed (Asterisk -> Gemini)
        # Gemini only accepts 16kHz input audio
        audio_to_send = audio_data

        if self.asterisk_to_gemini_resampler:
            audio_to_send = self.asterisk_to_gemini_resampler.resample(audio_data)
            logger.info(
                f"Resampled Asterisk audio: {len(audio_data)} -> {len(audio_to_send)} bytes"
            )
            self._debug_log(
                f"Asterisk resample: {len(audio_data)} -> {len(audio_to_send)} bytes"
            )

        # Gemini input MUST be 16kHz (see https://ai.google.dev/gemini-api/docs/live)
        mime_rate = 16000

        # Use send_realtime_input() for streaming audio to Gemini Live API
        # The audio dict must contain 'data' (bytes) and 'mime_type'
        # IMPORTANT: MIME type MUST include sample rate (e.g., audio/pcm;rate=16000)
        # Without the rate parameter, Gemini may misinterpret the audio causing distortion
        mime_type = f"audio/pcm;rate={mime_rate}"

        await self._dump_audio("gemini_in", audio_to_send)
        await self._trace_event(
            "media",
            "fa->gemini",
            bytes=len(audio_to_send),
            b64=self._encode_bytes(audio_to_send),
            mime_type=mime_type,
            resampled=self.asterisk_to_gemini_resampler is not None,
            hex_prefix=audio_to_send[:16].hex(),
        )
        await self._trace_event(
            "gemini_send",
            "fa->gemini",
            payload=self._safe_serialize({
                "audio": {"data": audio_to_send, "mime_type": mime_type}
            }),
        )
        audio_blob = Blob(data=audio_to_send, mime_type=mime_type)
        await self.gemini_session.send_realtime_input(audio=audio_blob)

    async def _receive_from_gemini(self) -> None:
        """Receive audio from Gemini and send to Asterisk."""
        if not self.gemini_session:
            return

        try:
            async for response in self._iter_gemini_messages():
                if response.tool_call:
                    await self._handle_tool_call(response.tool_call)
                await self._trace_event(
                    "gemini_message",
                    "gemini->fa",
                    payload=self._safe_serialize(response),
                )
                # Log transcripts for debugging
                if response.server_content:
                    # Log input (user) transcription
                    if response.server_content.input_transcription:
                        text = response.server_content.input_transcription.text
                        if text:
                            logger.info(f"[TRANSCRIPT] User: {text}")
                            await self._trace_event(
                                "transcript",
                                "gemini->fa",
                                speaker="user",
                                text=text,
                            )

                    # Log output (model) transcription
                    if response.server_content.output_transcription:
                        text = response.server_content.output_transcription.text
                        if text:
                            logger.info(f"[TRANSCRIPT] Assistant: {text}")
                            await self._trace_event(
                                "transcript",
                                "gemini->fa",
                                speaker="assistant",
                                text=text,
                            )

                # Extract audio data
                server_content = response.server_content
                if server_content and server_content.model_turn:
                    parts = server_content.model_turn.parts or []
                    for part_index, part in enumerate(parts):
                        part_info: dict[str, JsonValue] = {
                            "part_index": part_index,
                        }
                        part_text = getattr(part, "text", None)
                        if part_text:
                            part_info["text"] = part_text

                        function_call = getattr(part, "function_call", None)
                        if function_call:
                            part_info["function_call"] = self._safe_serialize(
                                function_call
                            )

                        function_response = getattr(part, "function_response", None)
                        if function_response:
                            part_info["function_response"] = self._safe_serialize(
                                function_response
                            )

                        inline_data = getattr(part, "inline_data", None)
                        if inline_data and getattr(inline_data, "data", None):
                            inline_bytes = bytes(inline_data.data)
                            part_info["inline_data"] = {
                                "mime_type": getattr(inline_data, "mime_type", None),
                                "bytes": len(inline_bytes),
                                "b64": self._encode_bytes(inline_bytes),
                            }

                        await self._trace_event(
                            "gemini_part",
                            "gemini->fa",
                            payload=self._safe_serialize(part_info),
                        )

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
                            self.assistant_speaking_until = time.monotonic() + (
                                self.assistant_duck_ms / 1000.0
                            )
                            await self._trace_event(
                                "media",
                                "gemini->fa",
                                bytes=len(audio_data),
                                b64=self._encode_bytes(audio_data),
                                mime_type=part.inline_data.mime_type,
                                hex_prefix=audio_data[:16].hex(),
                            )
                            await self._dump_audio("gemini_out", audio_data)

                            # Resample if needed (Gemini -> Asterisk)
                            target_audio = audio_data
                            if self.gemini_to_asterisk_resampler:
                                target_audio = (
                                    self.gemini_to_asterisk_resampler.resample(
                                        audio_data
                                    )
                                )
                                self._debug_log(
                                    "Gemini resample: "
                                    f"{len(audio_data)} -> {len(target_audio)} bytes"
                                )

                            # Buffer and send
                            await self._dump_audio("asterisk_out", target_audio)
                            self.audio_buffer.extend(target_audio)

                            while len(self.audio_buffer) >= self.send_frame_size:
                                if not self.media_send_allowed.is_set():
                                    self._debug_log(
                                        "Asterisk media flow paused; waiting for MEDIA_XON"
                                    )
                                    await self._trace_event(
                                        "flow_control",
                                        "fa",
                                        state="waiting_for_xon",
                                        buffer_len=len(self.audio_buffer),
                                    )
                                await self.media_send_allowed.wait()
                                chunk = self.audio_buffer[: self.send_frame_size]
                                del self.audio_buffer[: self.send_frame_size]
                                now = time.monotonic()
                                if self._pacing_last_send_ts:
                                    interval_ms = (
                                        now - self._pacing_last_send_ts
                                    ) * 1000.0
                                    self._pacing_interval_sum_ms += interval_ms
                                    self._pacing_interval_max_ms = max(
                                        self._pacing_interval_max_ms, interval_ms
                                    )
                                    self._pacing_samples += 1
                                    if self._pacing_samples % _PACE_LOG_EVERY == 0:
                                        avg_ms = (
                                            self._pacing_interval_sum_ms
                                            / self._pacing_samples
                                        )
                                        await self._trace_event(
                                            "pacing",
                                            "fa->asterisk",
                                            avg_interval_ms=round(avg_ms, 3),
                                            max_interval_ms=round(
                                                self._pacing_interval_max_ms, 3
                                            ),
                                            frame_bytes=self.send_frame_size,
                                            frame_duration_ms=round(
                                                self.send_frame_duration_ms, 3
                                            ),
                                            buffer_len=len(self.audio_buffer),
                                            underflows=self._pacing_underflows,
                                        )
                                        self._pacing_interval_sum_ms = 0.0
                                        self._pacing_interval_max_ms = 0.0
                                        self._pacing_samples = 0
                                        self._pacing_underflows = 0
                                self._pacing_last_send_ts = now
                                await self._trace_event(
                                    "media",
                                    "fa->asterisk",
                                    bytes=len(chunk),
                                    b64=self._encode_bytes(chunk),
                                    hex_prefix=chunk[:16].hex(),
                                    buffer_len=len(self.audio_buffer),
                                )
                                await self.websocket.send_bytes(bytes(chunk))
                                if len(self.audio_buffer) < self.send_frame_size:
                                    elapsed_ms = (
                                        time.monotonic() - self._pacing_last_send_ts
                                    ) * 1000.0
                                    delay_ms = self.send_frame_duration_ms - elapsed_ms
                                    if delay_ms > 0:
                                        self._pacing_underflows += 1
                                        await asyncio.sleep(delay_ms / 1000.0)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error receiving from Gemini: {e}", exc_info=True)
            await self._trace_event(
                "error",
                "fa",
                error=str(e),
            )

    async def _handle_tool_call(self, tool_call: LiveServerToolCall) -> None:
        if not self.gemini_session:
            return

        function_calls: list[FunctionCall] = tool_call.function_calls or []
        if not function_calls:
            return

        if not self.processing_service or not self.processing_service.tools_provider:
            provider_responses: list[FunctionResponse] = []
            for call in function_calls:
                call_id = call.id
                name = call.name or "unknown"
                provider_responses.append(
                    FunctionResponse(
                        id=call_id,
                        name=name,
                        response={"error": "Tools provider not available."},
                    )
                )
            await self.gemini_session.send_tool_response(
                function_responses=provider_responses
            )
            await self._trace_event(
                "tool_response",
                "fa->gemini",
                payload=self._safe_serialize(provider_responses),
            )
            return

        if self.database_engine is None:
            engine_responses: list[FunctionResponse] = []
            for call in function_calls:
                call_id = call.id
                name = call.name or "unknown"
                engine_responses.append(
                    FunctionResponse(
                        id=call_id,
                        name=name,
                        response={"error": "Database engine not available."},
                    )
                )
            await self.gemini_session.send_tool_response(
                function_responses=engine_responses
            )
            await self._trace_event(
                "tool_response",
                "fa->gemini",
                payload=self._safe_serialize(engine_responses),
            )
            return

        # TODO(andrew): Consider parallel tool execution and NON_BLOCKING tool calls.
        function_responses: list[FunctionResponse] = []
        for call in function_calls:
            call_id = call.id
            name = call.name
            args_raw = call.args
            if isinstance(args_raw, dict):
                args: dict[str, JsonValue] = {
                    str(key): self._safe_serialize(value)
                    for key, value in args_raw.items()
                }
            else:
                args = {}

            if not name:
                function_responses.append(
                    FunctionResponse(
                        id=call_id,
                        name="unknown",
                        response={"error": "Tool call missing name."},
                    )
                )
                continue

            await self._trace_event(
                "tool_call",
                "gemini->fa",
                tool_name=name,
                call_id=call_id,
                args=self._safe_serialize(args),
            )

            try:
                async with get_db_context(self.database_engine) as db_context:
                    exec_context = ToolExecutionContext(
                        interface_type="telephone",
                        conversation_id=self.conversation_id,
                        user_name=self.extension or "Caller",
                        user_id=None,
                        turn_id=call_id,
                        db_context=db_context,
                        chat_interface=None,
                        chat_interfaces=None,
                        timezone_str=self.processing_service.timezone_str,
                        processing_profile_id=self.processing_service.service_config.id,
                        subconversation_id=None,
                        request_confirmation_callback=None,
                        processing_service=self.processing_service,
                        clock=self.processing_service.clock,
                        home_assistant_client=self.processing_service.home_assistant_client,
                        event_sources=self.processing_service.event_sources,
                        indexing_source=(
                            self.processing_service.event_sources.get("indexing")
                            if self.processing_service.event_sources
                            else None
                        ),
                        attachment_registry=self.processing_service.attachment_registry,
                        camera_backend=self.processing_service.camera_backend,
                        tools_provider=self.processing_service.tools_provider,
                    )

                    result = await self.processing_service.tools_provider.execute_tool(
                        name, args, exec_context, call_id
                    )

                if isinstance(result, ToolResult):
                    if result.data is not None:
                        response_payload: dict[str, JsonValue] = {
                            "output": self._safe_serialize(result.data)
                        }
                        if result.text:
                            response_payload["text"] = result.text
                    else:
                        response_payload = {"output": result.text or ""}
                else:
                    response_payload = {"output": self._safe_serialize(result)}

                function_responses.append(
                    FunctionResponse(
                        id=call_id,
                        name=name,
                        response=response_payload,
                    )
                )
            except Exception as e:
                logger.error(f"Tool execution failed for '{name}': {e}", exc_info=True)
                function_responses.append(
                    FunctionResponse(
                        id=call_id,
                        name=name,
                        response={"error": str(e)},
                    )
                )

        if function_responses:
            await self.gemini_session.send_tool_response(
                function_responses=function_responses
            )
            await self._trace_event(
                "tool_response",
                "fa->gemini",
                payload=self._safe_serialize(function_responses),
            )

    async def _iter_gemini_messages(self) -> AsyncIterator[LiveServerMessage]:
        if not self.gemini_session:
            return

        while True:
            if self.websocket.client_state != WebSocketState.CONNECTED:
                return

            received_any = False
            async for response in self.gemini_session.receive():
                received_any = True
                yield response

            if not received_any:
                return


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
    tools: ToolListUnion | None = None

    try:
        processing_services = getattr(websocket.app.state, "processing_services", {})
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
                tools = cast("ToolListUnion", convert_tools_to_genai_format(raw_tools))
                logger.info(
                    f"Loaded {len(tools)} tools (Gemini format) for telephone profile"
                )
        else:
            logger.warning("Telephone profile not found, using default configuration")

    except Exception as e:
        logger.error(
            f"Error loading telephone profile configuration: {e}", exc_info=True
        )

    handler = AsteriskLiveHandler(
        websocket,
        client,
        gemini_live_config=gemini_live_config,
        processing_service=telephone_service,
        extension=extension,
        conversation_id=conversation_id,
        system_instruction=system_instruction,
        tools=tools,
    )
    await handler.run()
