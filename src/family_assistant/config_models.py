"""Pydantic models for application configuration.

This module provides type-safe configuration models that replace untyped dict access.
Using Pydantic models ensures:
1. Typos in configuration property names are caught at load time
2. Type validation for all configuration values
3. Better IDE support with autocomplete
4. Clear documentation of the configuration schema

Configuration priority (lowest to highest):
1. Code defaults (defined in model Field defaults)
2. config.yaml file
3. Environment variables
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RetryModelConfig(BaseModel):
    """Configuration for a single model in retry/fallback chain."""

    model_config = ConfigDict(extra="forbid")

    provider: str | None = None
    model: str | None = None


class RetryConfig(BaseModel):
    """Configuration for LLM retry/fallback behavior."""

    model_config = ConfigDict(extra="forbid")

    primary: RetryModelConfig = Field(default_factory=RetryModelConfig)
    fallback: RetryModelConfig | None = None


class ReolinkCameraItemConfig(BaseModel):
    """Configuration for a single Reolink camera."""

    model_config = ConfigDict(extra="forbid")

    host: str
    username: str
    password: str
    port: int | None = None  # None means auto-detect based on use_https
    use_https: bool = True
    channel: int = 0
    name: str | None = None
    prefer_download: bool = (
        False  # Skip FLV streaming, use direct download (faster for TLS issues)
    )

    @property
    def effective_port(self) -> int:
        """Get the effective port, defaulting based on use_https if not set."""
        if self.port is not None:
            return self.port
        return 443 if self.use_https else 80


class CameraConfig(BaseModel):
    """Camera backend configuration.

    Can be configured per-profile (e.g., camera_analyst profile) to enable
    camera tools. Currently supports 'reolink' backend.
    """

    model_config = ConfigDict(extra="forbid")

    backend: str = "reolink"  # Currently only 'reolink' is supported
    cameras_config: dict[str, ReolinkCameraItemConfig] = Field(default_factory=dict)


class ProcessingConfig(BaseModel):
    """Configuration for message processing behavior.

    This is used within service profiles to configure LLM behavior,
    history handling, and other processing parameters.
    """

    model_config = ConfigDict(extra="forbid")

    prompts: dict[str, str] = Field(default_factory=dict)
    timezone: str = "UTC"
    max_history_messages: int = 5
    history_max_age_hours: float = 24.0
    web_max_history_messages: int | None = None
    web_history_max_age_hours: float | None = None
    llm_model: str | None = None
    provider: str | None = None  # 'google', 'openai', 'litellm'
    retry_config: RetryConfig | None = None
    delegation_security_level: str = "confirm"  # "blocked", "confirm", "unrestricted"
    home_assistant_api_url: str | None = None
    home_assistant_token: str | None = None
    home_assistant_context_template: str | None = None
    home_assistant_verify_ssl: bool = True
    include_system_docs: list[str] | None = None
    max_iterations: int = 5
    calendar_config: CalendarConfig | None = None  # Per-profile calendar config
    camera_config: CameraConfig | None = None  # Per-profile camera backend config


class ToolsConfig(BaseModel):
    """Configuration for tool availability and behavior.

    Controls which tools are enabled, which require confirmation,
    and MCP server settings.
    """

    model_config = ConfigDict(extra="forbid")

    enable_local_tools: list[str] | None = None
    enable_mcp_server_ids: list[str] | None = None
    confirm_tools: list[str] = Field(default_factory=list)
    mcp_initialization_timeout_seconds: int = 60
    confirmation_timeout_seconds: float = 3600.0


class ServiceProfile(BaseModel):
    """Configuration for a service profile.

    Service profiles allow different assistant behaviors for different
    contexts (e.g., browser profile, research profile, reminder profile).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    description: str = ""
    processing_config: ProcessingConfig = Field(default_factory=ProcessingConfig)
    tools_config: ToolsConfig = Field(default_factory=ToolsConfig)
    chat_id_to_name_map: dict[int, str] = Field(default_factory=dict)
    slash_commands: list[str] = Field(default_factory=list)


class DefaultProfileSettings(BaseModel):
    """Default settings applied to all profiles unless overridden."""

    model_config = ConfigDict(extra="forbid")

    processing_config: ProcessingConfig = Field(default_factory=ProcessingConfig)
    tools_config: ToolsConfig = Field(default_factory=ToolsConfig)
    chat_id_to_name_map: dict[int, str] = Field(default_factory=dict)
    slash_commands: list[str] = Field(default_factory=list)


class CalDAVConfig(BaseModel):
    """CalDAV server configuration."""

    model_config = ConfigDict(extra="forbid")

    username: str | None = None
    password: str | None = None
    calendar_urls: list[str] = Field(default_factory=list)
    base_url: str | None = None


class ICalConfig(BaseModel):
    """iCal URL configuration."""

    model_config = ConfigDict(extra="forbid")

    urls: list[str] = Field(default_factory=list)


class DuplicateDetectionEmbeddingConfig(BaseModel):
    """Embedding settings for duplicate detection."""

    model_config = ConfigDict(extra="forbid")

    model: str = "sentence-transformers/all-MiniLM-L6-v2"
    device: str = "cpu"


class DuplicateDetectionConfig(BaseModel):
    """Calendar duplicate event detection settings."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    similarity_strategy: str = "embedding"  # "embedding" or "fuzzy"
    similarity_threshold: float = 0.30
    time_window_hours: int = 2
    embedding: DuplicateDetectionEmbeddingConfig = Field(
        default_factory=DuplicateDetectionEmbeddingConfig
    )


class CalendarConfig(BaseModel):
    """Calendar integration configuration."""

    model_config = ConfigDict(extra="forbid")

    caldav: CalDAVConfig | None = None
    ical: ICalConfig | None = None
    duplicate_detection: DuplicateDetectionConfig = Field(
        default_factory=DuplicateDetectionConfig
    )


class PWAConfig(BaseModel):
    """PWA and push notification configuration."""

    model_config = ConfigDict(extra="forbid")

    vapid_public_key: str | None = None
    vapid_private_key: str | None = None
    vapid_contact_email: str | None = None


class GeminiVoiceConfig(BaseModel):
    """Gemini voice settings."""

    model_config = ConfigDict(extra="forbid")

    name: str = "Puck"


class GeminiSessionConfig(BaseModel):
    """Gemini session settings."""

    model_config = ConfigDict(extra="forbid")

    max_duration_minutes: int = 15


class GeminiTranscriptionConfig(BaseModel):
    """Gemini transcription settings."""

    model_config = ConfigDict(extra="forbid")

    input_enabled: bool = True
    output_enabled: bool = True


class GeminiVADConfig(BaseModel):
    """Gemini Voice Activity Detection settings."""

    model_config = ConfigDict(extra="forbid")

    automatic: bool = True
    start_of_speech_sensitivity: str = "DEFAULT"
    end_of_speech_sensitivity: str = "DEFAULT"
    prefix_padding_ms: int | None = None
    silence_duration_ms: int | None = None


class GeminiAffectiveDialogConfig(BaseModel):
    """Gemini affective dialog settings."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False


class GeminiProactivityConfig(BaseModel):
    """Gemini proactivity settings."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    proactive_audio: bool = False


class GeminiThinkingConfig(BaseModel):
    """Gemini thinking/reasoning settings."""

    model_config = ConfigDict(extra="forbid")

    include_thoughts: bool = False


class TelephoneVADOverrides(BaseModel):
    """VAD overrides for telephone calls."""

    model_config = ConfigDict(extra="forbid")

    start_of_speech_sensitivity: str = "START_SENSITIVITY_HIGH"
    end_of_speech_sensitivity: str = "DEFAULT"
    silence_duration_ms: int | None = 1000


class TelephoneOverrides(BaseModel):
    """Telephone-specific overrides for Gemini Live API."""

    model_config = ConfigDict(extra="forbid")

    vad: TelephoneVADOverrides = Field(default_factory=TelephoneVADOverrides)


class GeminiLiveConfig(BaseModel):
    """Gemini Live Voice API configuration."""

    model_config = ConfigDict(extra="forbid")

    model: str = "gemini-2.5-flash-native-audio-preview-09-2025"
    voice: GeminiVoiceConfig = Field(default_factory=GeminiVoiceConfig)
    session: GeminiSessionConfig = Field(default_factory=GeminiSessionConfig)
    transcription: GeminiTranscriptionConfig = Field(
        default_factory=GeminiTranscriptionConfig
    )
    vad: GeminiVADConfig = Field(default_factory=GeminiVADConfig)
    affective_dialog: GeminiAffectiveDialogConfig = Field(
        default_factory=GeminiAffectiveDialogConfig
    )
    proactivity: GeminiProactivityConfig = Field(
        default_factory=GeminiProactivityConfig
    )
    thinking: GeminiThinkingConfig = Field(default_factory=GeminiThinkingConfig)
    telephone_overrides: TelephoneOverrides = Field(default_factory=TelephoneOverrides)


class IndexingProcessorConfig(BaseModel):
    """Configuration for a single indexing processor."""

    model_config = ConfigDict(extra="allow")

    type: str
    # ast-grep-ignore: no-dict-any - Processor configs are genuinely arbitrary
    config: dict[str, Any] = Field(default_factory=dict)


class IndexingPipelineConfig(BaseModel):
    """Document indexing pipeline configuration."""

    model_config = ConfigDict(extra="forbid")

    processors: list[IndexingProcessorConfig] = Field(default_factory=list)


class AttachmentConfig(BaseModel):
    """Attachment handling configuration."""

    model_config = ConfigDict(extra="forbid")

    max_file_size: int = 104857600  # 100MB
    max_multimodal_size: int = 20971520  # 20MB
    storage_path: str = "/tmp/chat_attachments"
    allowed_mime_types: list[str] = Field(
        default_factory=lambda: [
            "image/jpeg",
            "image/png",
            "image/gif",
            "image/webp",
            "image/bmp",
            "image/tiff",
            "application/pdf",
            "text/plain",
            "application/json",
            "text/csv",
            "video/mp4",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ]
    )


class EventStorageConfig(BaseModel):
    """Event system storage configuration."""

    model_config = ConfigDict(extra="forbid")

    sample_interval_hours: float = 1.0
    max_event_size: int = 100000  # 100KB
    retention_hours: int = 48


class HomeAssistantSourceConfig(BaseModel):
    """Home Assistant event source configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class WebhookSourceConfig(BaseModel):
    """Webhook event source configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    secrets: dict[str, str] = Field(
        default_factory=dict,
        description="Optional per-source secrets for signature verification. "
        "Keys are source names, values are secret keys.",
    )


class EventSourcesConfig(BaseModel):
    """Event sources configuration."""

    model_config = ConfigDict(extra="forbid")

    home_assistant: HomeAssistantSourceConfig = Field(
        default_factory=HomeAssistantSourceConfig
    )
    webhook: WebhookSourceConfig = Field(default_factory=WebhookSourceConfig)


class EventSystemConfig(BaseModel):
    """Event system configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    storage: EventStorageConfig = Field(default_factory=EventStorageConfig)
    sources: EventSourcesConfig = Field(default_factory=EventSourcesConfig)


class MessageBatchingConfig(BaseModel):
    """Message batching configuration for Telegram."""

    model_config = ConfigDict(extra="forbid")

    strategy: str = "none"
    delay_seconds: float = 0.5


class DatabaseErrorsLoggingConfig(BaseModel):
    """Configuration for database error logging."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    retention_days: int = 30


class LoggingConfig(BaseModel):
    """Logging configuration."""

    model_config = ConfigDict(extra="forbid")

    database_errors: DatabaseErrorsLoggingConfig = Field(
        default_factory=DatabaseErrorsLoggingConfig
    )


class MCPServerConfig(BaseModel):
    """Configuration for a single MCP server.

    Uses extra="allow" to support arbitrary server-specific configuration.
    """

    model_config = ConfigDict(extra="allow")

    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class MCPConfig(BaseModel):
    """MCP (Model Context Protocol) servers configuration."""

    model_config = ConfigDict(extra="forbid")

    mcpServers: dict[str, MCPServerConfig] = Field(default_factory=dict)


class OIDCConfig(BaseModel):
    """OpenID Connect authentication configuration."""

    model_config = ConfigDict(extra="forbid")

    client_id: str = ""
    client_secret: str = ""
    discovery_url: str = ""


class AppConfig(BaseModel):
    """Main application configuration.

    This is the root configuration model that contains all application settings.
    Property access is type-safe - misspelled property names will raise AttributeError.
    """

    model_config = ConfigDict(extra="forbid")

    # Secrets and API keys (primarily from environment)
    telegram_token: str | None = None
    telegram_enabled: bool = True
    openrouter_api_key: str | None = None
    gemini_api_key: str | None = None

    # User access control
    allowed_user_ids: list[int] = Field(default_factory=list)
    developer_chat_id: int | None = None

    # Model configuration
    model: str = "gemini/gemini-2.5-pro"
    embedding_model: str = "gemini/gemini-embedding-001"
    embedding_dimensions: int = 1536

    # Storage paths
    database_url: str = "sqlite+aiosqlite:///family_assistant.db"
    server_url: str = "http://localhost:8000"
    document_storage_path: str = "/mnt/data/files"
    attachment_storage_path: str = "/mnt/data/mailbox/attachments"
    mailbox_raw_dir: str | None = None  # Directory for saving raw email requests
    chat_attachment_storage_path: str | None = (
        None  # Falls back to attachment_config.storage_path
    )

    # Weather integration
    willyweather_api_key: str | None = None
    willyweather_location_id: int | None = None

    # Debug flags
    litellm_debug: bool = False
    debug_llm_messages: bool = False
    dev_mode: bool = False

    # Authentication
    oidc: OIDCConfig = Field(default_factory=OIDCConfig)

    # Service profiles
    default_service_profile_id: str = "default_assistant"
    service_profiles: list[ServiceProfile] = Field(default_factory=list)
    default_profile_settings: DefaultProfileSettings = Field(
        default_factory=DefaultProfileSettings
    )

    # Feature configurations
    calendar_config: CalendarConfig = Field(default_factory=CalendarConfig)
    pwa_config: PWAConfig = Field(default_factory=PWAConfig)
    gemini_live_config: GeminiLiveConfig = Field(default_factory=GeminiLiveConfig)
    mcp_config: MCPConfig = Field(default_factory=MCPConfig)
    indexing_pipeline_config: IndexingPipelineConfig = Field(
        default_factory=IndexingPipelineConfig
    )
    attachment_config: AttachmentConfig = Field(default_factory=AttachmentConfig)
    event_system: EventSystemConfig = Field(default_factory=EventSystemConfig)
    message_batching_config: MessageBatchingConfig = Field(
        default_factory=MessageBatchingConfig
    )

    # LLM parameters (pattern -> parameters mapping)
    # ast-grep-ignore: no-dict-any - LLM params are provider-specific and genuinely arbitrary
    llm_parameters: dict[str, dict[str, Any]] = Field(default_factory=dict)

    # Logging configuration
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    # Server port (optional, defaults to 8000)
    server_port: int = 8000

    # Attachment selection thresholds (global)
    attachment_selection_threshold: int = 3  # Trigger selection when > this many
    max_response_attachments: int = 6  # Max attachments per response
