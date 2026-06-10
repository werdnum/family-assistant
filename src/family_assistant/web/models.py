from datetime import datetime
from typing import TYPE_CHECKING, Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from family_assistant.llm.messages import MessageAttachmentMetadata

if TYPE_CHECKING:
    from starlette.datastructures import State

    from family_assistant.config_models import AppConfig


class ChatAttachmentRequest(TypedDict, total=False):
    """Attachment data sent from the frontend in chat requests."""

    type: str
    content: str
    mime_type: str
    filename: str


class ToolCallFunctionResponse(TypedDict, total=False):
    """Function details within a tool call response (OpenAI format)."""

    name: str
    arguments: str


class ToolCallResponseItem(TypedDict, total=False):
    """Tool call data included in chat API responses (OpenAI format)."""

    id: str
    type: str
    function: ToolCallFunctionResponse


# --- Gemini Live Voice API Configuration Models ---
class GeminiLiveVoiceConfig(BaseModel):
    """Voice configuration for Gemini Live."""

    name: str = "Puck"


class GeminiLiveSessionConfig(BaseModel):
    """Session limits for Gemini Live."""

    max_duration_minutes: int = 15


class GeminiLiveTranscriptionConfig(BaseModel):
    """Transcription settings for Gemini Live."""

    input_enabled: bool = True
    output_enabled: bool = True


class GeminiLiveVADConfig(BaseModel):
    """Voice Activity Detection configuration for Gemini Live."""

    automatic: bool = True
    start_of_speech_sensitivity: str = "DEFAULT"
    end_of_speech_sensitivity: str = "DEFAULT"
    prefix_padding_ms: int | None = None
    silence_duration_ms: int | None = None


class GeminiLiveAffectiveDialogConfig(BaseModel):
    """Affective dialog configuration for Gemini Live."""

    enabled: bool = False


class GeminiLiveProactivityConfig(BaseModel):
    """Proactivity configuration for Gemini Live."""

    enabled: bool = False
    proactive_audio: bool = False


class GeminiLiveThinkingConfig(BaseModel):
    """Thinking configuration for Gemini Live."""

    include_thoughts: bool = False


class GeminiLiveGreetingConfig(BaseModel):
    """Configuration for pre-recorded greeting played on call answer."""

    enabled: bool = True
    wav_path: str | None = None


class GeminiLiveConfig(BaseModel):
    """Full Gemini Live Voice API configuration."""

    model: str = "gemini-3.1-flash-live-preview"
    voice: GeminiLiveVoiceConfig = GeminiLiveVoiceConfig()
    session: GeminiLiveSessionConfig = GeminiLiveSessionConfig()
    transcription: GeminiLiveTranscriptionConfig = GeminiLiveTranscriptionConfig()
    vad: GeminiLiveVADConfig = GeminiLiveVADConfig()
    affective_dialog: GeminiLiveAffectiveDialogConfig = (
        GeminiLiveAffectiveDialogConfig()
    )
    proactivity: GeminiLiveProactivityConfig = GeminiLiveProactivityConfig()
    thinking: GeminiLiveThinkingConfig = GeminiLiveThinkingConfig()
    greeting: GeminiLiveGreetingConfig = GeminiLiveGreetingConfig()

    @classmethod
    # ast-grep-ignore: no-dict-any - Config dict from YAML has dynamic nested structure
    def from_dict(cls, config_dict: dict[str, Any]) -> "GeminiLiveConfig":
        """Create a GeminiLiveConfig from a dictionary (e.g., from config.yaml)."""
        return cls(
            model=config_dict.get("model", cls.model_fields["model"].default),
            voice=GeminiLiveVoiceConfig(**config_dict.get("voice", {})),
            session=GeminiLiveSessionConfig(**config_dict.get("session", {})),
            transcription=GeminiLiveTranscriptionConfig(
                **config_dict.get("transcription", {})
            ),
            vad=GeminiLiveVADConfig(**config_dict.get("vad", {})),
            affective_dialog=GeminiLiveAffectiveDialogConfig(
                **config_dict.get("affective_dialog", {})
            ),
            proactivity=GeminiLiveProactivityConfig(
                **config_dict.get("proactivity", {})
            ),
            thinking=GeminiLiveThinkingConfig(**config_dict.get("thinking", {})),
            greeting=GeminiLiveGreetingConfig(**config_dict.get("greeting", {})),
        )

    @classmethod
    def from_app_state(
        cls,
        app_state: "State",
    ) -> "GeminiLiveConfig":
        """Get the Gemini Live configuration from app state.

        This is a convenience method to avoid duplicating config retrieval logic.
        """
        config: AppConfig | None = getattr(app_state, "config", None)
        if config is None:
            return cls.from_dict({})
        # Access gemini_live_config attribute from AppConfig
        gemini_live_config = config.gemini_live_config
        return cls.from_dict(gemini_live_config.model_dump())

    @classmethod
    def from_app_state_with_telephone_overrides(
        cls,
        app_state: "State",
    ) -> "GeminiLiveConfig":
        """Get the Gemini Live configuration with telephone-specific overrides applied.

        This loads the base configuration and then applies any overrides from the
        'telephone_overrides' section. Telephone audio has different characteristics
        (narrowband, potential noise) that may require different VAD settings.
        """
        app_config: AppConfig | None = getattr(app_state, "config", None)
        if app_config is None:
            return cls.from_dict({})

        gemini_live_config = app_config.gemini_live_config
        gemini_live_config_dict = gemini_live_config.model_dump()

        # Start with base config
        base_config = cls.from_dict(gemini_live_config_dict)

        # Apply telephone-specific overrides if present
        overrides = gemini_live_config_dict.get("telephone_overrides", {})
        if overrides:
            # Deep merge VAD overrides
            if "vad" in overrides:
                vad_dict = base_config.vad.model_dump()
                for key, value in overrides["vad"].items():
                    if value is not None:
                        vad_dict[key] = value
                base_config = base_config.model_copy(
                    update={"vad": GeminiLiveVADConfig(**vad_dict)}
                )

            # Add other override sections as needed (voice, session, etc.)
            if "voice" in overrides:
                voice_dict = base_config.voice.model_dump()
                for key, value in overrides["voice"].items():
                    if value is not None:
                        voice_dict[key] = value
                base_config = base_config.model_copy(
                    update={"voice": GeminiLiveVoiceConfig(**voice_dict)}
                )

            if "greeting" in overrides:
                greeting_dict = base_config.greeting.model_dump()
                for key, value in overrides["greeting"].items():
                    if value is not None:
                        greeting_dict[key] = value
                base_config = base_config.model_copy(
                    update={"greeting": GeminiLiveGreetingConfig(**greeting_dict)}
                )

        return base_config


# --- Pydantic model for search results (optional but good practice) ---
class SearchResultItem(BaseModel):
    embedding_id: int
    document_id: int
    title: str | None
    source_type: str
    source_id: str | None = None
    source_uri: str | None = None
    created_at: datetime | None
    embedding_type: str
    embedding_source_content: str | None
    chunk_index: int | None = None
    # ast-grep-ignore: no-dict-any - Document metadata has dynamic structure varying by source type
    doc_metadata: dict[str, Any] | None = None
    distance: float | None = None
    fts_score: float | None = None
    rrf_score: float | None = None

    model_config = ConfigDict(from_attributes=True)


# --- Pydantic model for API response ---
class DocumentUploadResponse(BaseModel):
    message: str
    document_id: int
    task_enqueued: bool


# --- API Token Models ---
class ApiTokenCreateRequest(BaseModel):
    name: str
    expires_at: str | None = (
        None  # ISO 8601 format string, e.g., "YYYY-MM-DDTHH:MM:SSZ"
    )


class ApiTokenCreateResponse(BaseModel):
    id: int
    name: str
    full_token: str  # The full, unhashed token (prefix + secret)
    prefix: str
    user_identifier: str
    created_at: datetime
    expires_at: datetime | None = None
    is_revoked: bool
    last_used_at: datetime | None = None


# --- API Chat Models ---
class ChatPromptRequest(BaseModel):
    prompt: str
    conversation_id: str | None = None
    profile_id: str | None = None  # Added to specify processing profile
    interface_type: str | None = None  # Interface type (e.g., 'web', 'api', 'mobile')
    attachments: list[ChatAttachmentRequest] | None = None
    turn_id: str | None = Field(
        default=None,
        description=(
            "Optional client-supplied UUIDv4 for idempotency. Retrying "
            "/send_message with the same turn_id returns the existing reply "
            "instead of re-driving the LLM."
        ),
    )


class ChatMessageResponse(BaseModel):
    reply: str  # Back to original field name to minimize disruption
    conversation_id: str
    turn_id: str
    attachments: list[MessageAttachmentMetadata] | None = None  # Add attachments field
    tool_calls: list[ToolCallResponseItem] | None = None
    already_complete: bool = Field(
        default=False,
        description=(
            "True when the reply was served from an existing turn (idempotent "
            "retry) rather than freshly generated."
        ),
    )


# --- App Auth / Token Exchange Models ---
class CodeExchangeRequest(BaseModel):
    code: str
    code_verifier: str


class CodeExchangeResponse(BaseModel):
    api_token: str
    refresh_token: str
    expires_in: int  # seconds


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class RefreshTokenResponse(BaseModel):
    api_token: str
    expires_in: int  # seconds


class TokenSessionResponse(BaseModel):
    ok: bool


class WebhookEventPayload(BaseModel):
    """Payload for generic webhook events."""

    model_config = ConfigDict(extra="allow")

    event_type: str | None = Field(
        default=None,
        description="Type/category of the event (e.g., 'alert', 'build'). "
        "Can also be provided via query parameter.",
    )
    source: str | None = Field(
        default=None,
        description="Identifier for the event source (e.g., 'grafana', 'github')",
    )
    title: str | None = Field(default=None, description="Human-readable event title")
    message: str | None = Field(default=None, description="Detailed event message")
    severity: str | None = Field(
        default=None, description="Severity level (e.g., 'info', 'warning', 'critical')"
    )
    # ast-grep-ignore: no-dict-any - Webhook data field intentionally accepts arbitrary structure from external sources
    data: dict[str, Any] | None = Field(
        default=None, description="Additional event-specific data"
    )
