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

import contextlib
import os
import zoneinfo
from contextvars import ContextVar
from email.utils import parseaddr
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

import cloudcoil.models.kubernetes.core.v1 as k8s_models  # noqa: TC002 - Pydantic needs at runtime
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from collections.abc import Generator

    from pydantic_settings import PydanticBaseSettingsSource

from .config_sources import DeepMergedYamlSource
from .delegation_security import DelegationSecurityLevel
from .tools.policy import (
    ToolPolicyConfig,  # noqa: TC001 - Pydantic resolves this model at runtime
)


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

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        try:
            zoneinfo.ZoneInfo(v)
        except (zoneinfo.ZoneInfoNotFoundError, KeyError) as e:
            msg = f"Invalid timezone '{v}'. Must be a valid IANA timezone (e.g. 'Australia/Sydney', 'America/New_York', 'UTC')."
            raise ValueError(msg) from e
        return v

    max_history_messages: int = 5
    history_max_age_hours: float = 24.0
    web_max_history_messages: int | None = None
    web_history_max_age_hours: float | None = None
    llm_model: str | None = None
    provider: str | None = None  # 'google', 'openai', 'anthropic'
    retry_config: RetryConfig | None = None
    delegation_security_level: DelegationSecurityLevel = DelegationSecurityLevel.CONFIRM
    allowed_delegation_sources: list[str] | None = None
    home_assistant_api_url: str | None = None
    home_assistant_token: str | None = None
    home_assistant_context_template: str | None = None
    home_assistant_verify_ssl: bool = True
    include_system_docs: list[str] | None = None
    max_iterations: int = 5
    context_pruning_min_turns: int = 3
    calendar_config: CalendarConfig | None = None  # Per-profile calendar config
    camera_config: CameraConfig | None = None  # Per-profile camera backend config
    greeting_wav_path: str | None = None
    default_note_visibility_labels: list[str] | None = None


class ToolsConfig(BaseModel):
    """Operational tool configuration.

    Tool access control lives in ``tools_policy``. This model only contains
    non-policy settings such as timeouts and optional on-demand catalog hints.
    """

    model_config = ConfigDict(extra="forbid")

    on_demand_local_tools: list[str] = Field(default_factory=list)
    on_demand_mcp_server_ids: list[str] = Field(default_factory=list)
    mcp_initialization_timeout_seconds: int = 60
    confirmation_timeout_seconds: float = 3600.0
    delegate_handoff_after_seconds: float = 15.0
    delegate_handoff_max_seconds: float = 120.0
    delegate_status_poll_seconds: float = 0.25

    def get_on_demand_tool_names(self) -> set[str]:
        """Return tool names configured for on-demand loading."""
        return set(self.on_demand_local_tools)

    def get_on_demand_mcp_server_ids(self) -> list[str]:
        """Return MCP server IDs configured for on-demand loading."""
        return list(self.on_demand_mcp_server_ids)


class RemoteA2AAuthConfig(BaseModel):
    """Auth configuration for a remote A2A agent (config-level)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["bearer", "api_key", "none"] = "none"
    token_env: str | None = None
    header_name: str = "Authorization"


class RemoteA2AConfig(BaseModel):
    """Configuration for a remote A2A agent profile."""

    model_config = ConfigDict(extra="forbid")

    agent_url: str
    auth: RemoteA2AAuthConfig = Field(default_factory=RemoteA2AAuthConfig)
    timeout_seconds: float = 300.0
    skills_description: str | None = None


class BrowserHandoffConfig(BaseModel):
    """Optional integration with an external browser-server (handoff service).

    When ``enabled`` and ``service_url`` are set, the semantic DOM browser tools
    run against a remote ``browser-server`` session instead of a local headless
    Playwright browser. The same rich tools (snapshot/click/fill/.../screenshot)
    work unchanged; the difference is the browser lives on a service that can
    transfer the live session to a human via noVNC (enabling
    ``browser_request_handoff``). Disabled by default — when off, browsing uses
    the in-process local Playwright backend exactly as before.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    # Cluster-internal base URL, e.g.
    # http://browser-server.browser-server.svc.cluster.local:8000
    service_url: str | None = None
    # Auth reuses the remote-A2A model: bearer token read from token_env at
    # request time (never stored in YAML).
    auth: RemoteA2AAuthConfig = Field(default_factory=RemoteA2AAuthConfig)
    timeout_seconds: float = 30.0
    # Profiles permitted to use the remote backend / request a human handoff.
    handoff_capable_profiles: list[str] = Field(
        default_factory=lambda: ["browser_profile"]
    )


class ServiceProfile(BaseModel):
    """Configuration for a service profile.

    Service profiles allow different assistant behaviors for different
    contexts (e.g., browser profile, research profile, reminder profile).
    When remote_a2a is set, the profile delegates to a remote A2A agent
    instead of running a local ProcessingService.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    description: str = ""
    processing_config: ProcessingConfig = Field(default_factory=ProcessingConfig)
    tools_config: ToolsConfig = Field(default_factory=ToolsConfig)
    tools_policy: ToolPolicyConfig | None = None
    operator_tools_policy: ToolPolicyConfig | None = Field(default=None, exclude=True)
    chat_id_to_name_map: dict[int, str] = Field(default_factory=dict)
    slash_commands: list[str] = Field(default_factory=list)
    visibility_grants: list[str] = Field(default_factory=list)
    remote_a2a: RemoteA2AConfig | None = None


class DefaultProfileSettings(BaseModel):
    """Default settings applied to all profiles unless overridden."""

    model_config = ConfigDict(extra="forbid")

    processing_config: ProcessingConfig = Field(default_factory=ProcessingConfig)
    tools_config: ToolsConfig = Field(default_factory=ToolsConfig)
    tools_policy: ToolPolicyConfig | None = None
    operator_tools_policy: ToolPolicyConfig | None = Field(default=None, exclude=True)
    chat_id_to_name_map: dict[int, str] = Field(default_factory=dict)
    slash_commands: list[str] = Field(default_factory=list)
    visibility_grants: list[str] = Field(default_factory=list)


class NotesConfig(BaseModel):
    """Configuration for notes visibility behavior."""

    model_config = ConfigDict(extra="forbid")

    default_visibility_labels: list[str] = Field(default_factory=list)


class SkillsConfig(BaseModel):
    """Configuration for file-based skills directories."""

    model_config = ConfigDict(extra="forbid")

    user_dir: str | None = None
    builtin_dir: str | None = None


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


class ApnsConfig(BaseModel):
    """Apple Push Notification service (iOS) configuration.

    The APNs sender is enabled only when team_id, key_id, bundle_id and a private key (either
    `auth_key` inline or `auth_key_path`) are all configured.
    """

    model_config = ConfigDict(extra="forbid")

    team_id: str | None = None
    key_id: str | None = None
    auth_key: str | None = None
    auth_key_path: str | None = None
    bundle_id: str | None = None
    use_sandbox: bool = False


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


class TelephoneGreetingConfig(BaseModel):
    """Configuration for pre-recorded greeting played on call answer."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    wav_path: str | None = None


class TelephoneOverrides(BaseModel):
    """Telephone-specific overrides for Gemini Live API."""

    model_config = ConfigDict(extra="forbid")

    vad: TelephoneVADOverrides = Field(default_factory=TelephoneVADOverrides)
    greeting: TelephoneGreetingConfig = Field(default_factory=TelephoneGreetingConfig)


class GeminiLiveConfig(BaseModel):
    """Gemini Live Voice API configuration."""

    model_config = ConfigDict(extra="forbid")

    model: str = "gemini-3.1-flash-live-preview"
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
    large_tool_result_threshold_kb: int = (
        100  # Auto-convert to attachment if > this size (in KiB)
    )
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
            "text/markdown",
            "application/json",
            "text/csv",
            "video/mp4",
            "video/webm",
            "video/ogg",
            "audio/mpeg",
            "audio/wav",
            "audio/ogg",
            "audio/webm",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ]
    )


def _normalize_string_set(values: object, label: str) -> set[str]:
    if values is None:
        return set()
    if not isinstance(values, (list, set, tuple)):
        msg = f"{label} values must be a list or set"
        raise TypeError(msg)

    normalized_values: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            msg = f"{label} values must be strings"
            raise TypeError(msg)
        normalized = value.strip()
        if not normalized:
            msg = f"Invalid {label}: {value!r}"
            raise ValueError(msg)
        normalized_values.add(normalized)
    return normalized_values


def _normalize_email_address_set(values: object, label: str) -> set[str]:
    if values is None:
        return set()
    if not isinstance(values, (list, set, tuple)):
        msg = f"{label} values must be a list or set"
        raise TypeError(msg)

    normalized_addresses: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            msg = f"{label} values must be strings"
            raise TypeError(msg)
        _, parsed_address = parseaddr(value)
        normalized = parsed_address.strip().lower()
        if not normalized:
            msg = f"Invalid {label}: {value!r}"
            raise ValueError(msg)
        normalized_addresses.add(normalized)
    return normalized_addresses


class EmailIntakeUserMapping(BaseModel):
    """Maps authorized inbound email addresses to an application user."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1)
    sender_addresses: set[str] = Field(default_factory=set)
    recipient_addresses: set[str] = Field(default_factory=set)

    @field_validator("sender_addresses", "recipient_addresses", mode="before")
    @classmethod
    def normalize_addresses(cls, values: object) -> set[str]:
        if values is None:
            return set()
        if not isinstance(values, (list, set, tuple)):
            msg = "Email intake mapping addresses must be a list or set"
            raise TypeError(msg)

        normalized_addresses: set[str] = set()
        for value in values:
            if not isinstance(value, str):
                msg = "Email intake mapping addresses must be strings"
                raise TypeError(msg)
            _, parsed_address = parseaddr(value)
            normalized = parsed_address.strip().lower()
            if not normalized:
                msg = f"Invalid email intake mapping address: {value!r}"
                raise ValueError(msg)
            normalized_addresses.add(normalized)
        return normalized_addresses

    @model_validator(mode="after")
    def require_at_least_one_address(self) -> EmailIntakeUserMapping:
        if not self.sender_addresses and not self.recipient_addresses:
            msg = "Email intake user mappings require at least one sender or recipient address"
            raise ValueError(msg)
        return self


class OIDCUserIdentityConfig(BaseModel):
    """OIDC identities associated with a canonical application user."""

    model_config = ConfigDict(extra="forbid")

    emails: set[str] = Field(default_factory=set)
    subjects: set[str] = Field(default_factory=set)

    @field_validator("emails", mode="before")
    @classmethod
    def normalize_emails(cls, values: object) -> set[str]:
        return _normalize_email_address_set(values, "OIDC email")

    @field_validator("subjects", mode="before")
    @classmethod
    def normalize_subjects(cls, values: object) -> set[str]:
        return _normalize_string_set(values, "OIDC subject")


class TelegramUserIdentityConfig(BaseModel):
    """Telegram identities associated with a canonical application user."""

    model_config = ConfigDict(extra="forbid")

    user_ids: set[int] = Field(default_factory=set)
    developer: bool = False


class EmailIntakeIdentityConfig(BaseModel):
    """Inbound email identities associated with a canonical application user."""

    model_config = ConfigDict(extra="forbid")

    sender_addresses: set[str] = Field(default_factory=set)
    recipient_addresses: set[str] = Field(default_factory=set)

    @field_validator("sender_addresses", "recipient_addresses", mode="before")
    @classmethod
    def normalize_addresses(cls, values: object) -> set[str]:
        return _normalize_email_address_set(values, "email intake address")


class UserIdentityConfig(BaseModel):
    """Canonical application user and the external identities that resolve to it."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    label: str | None = None
    oidc: OIDCUserIdentityConfig = Field(default_factory=OIDCUserIdentityConfig)
    telegram: TelegramUserIdentityConfig = Field(
        default_factory=TelegramUserIdentityConfig
    )
    email_intake: EmailIntakeIdentityConfig = Field(
        default_factory=EmailIntakeIdentityConfig
    )

    @field_validator("id")
    @classmethod
    def normalize_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            msg = "User id must not be empty"
            raise ValueError(msg)
        return normalized

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def require_identity(self) -> UserIdentityConfig:
        if (
            not self.oidc.emails
            and not self.oidc.subjects
            and not self.telegram.user_ids
            and not self.email_intake.sender_addresses
            and not self.email_intake.recipient_addresses
        ):
            msg = f"User {self.id!r} must configure at least one external identity"
            raise ValueError(msg)
        return self


class EmailIntakeConfig(BaseModel):
    """Security controls for inbound email webhooks.

    DKIM and DMARC are always evaluated locally against the raw MIME message that
    Mailgun forwards in the ``body-mime`` form field. When
    :attr:`require_authenticated_sender` is set, DMARC pass (aligned DKIM or SPF) is
    required before an email is accepted.
    """

    model_config = ConfigDict(extra="forbid")

    mailgun_webhook_signing_key: str | None = None
    mailgun_signature_max_age_seconds: int = Field(default=300, gt=0)
    allowed_sender_addresses: list[str] = Field(default_factory=list)
    allowed_recipient_addresses: list[str] = Field(default_factory=list)
    require_authenticated_sender: bool = False
    require_user_mapping: bool = False
    enable_actions: bool = False
    action_profile_id: str = "email_intake"
    user_mappings: list[EmailIntakeUserMapping] = Field(default_factory=list)
    max_raw_request_bytes: int = Field(default=25 * 1024 * 1024, gt=0)
    max_attachment_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    max_total_attachment_bytes: int = Field(default=25 * 1024 * 1024, gt=0)
    outbound_mailgun_api_key: str | None = None
    outbound_mailgun_domain: str | None = None
    outbound_from_address: str | None = None
    outbound_timeout_seconds: float = Field(default=10.0, gt=0)


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
    media_group_quiet_seconds: float = 1.0
    media_group_max_wait_seconds: float = 60.0


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
    tool_metadata: dict[str, list[str]] = Field(default_factory=dict)


class MCPConfig(BaseModel):
    """MCP (Model Context Protocol) servers configuration."""

    model_config = ConfigDict(extra="forbid")

    mcpServers: dict[str, MCPServerConfig] = Field(default_factory=dict)


class WorkerResourceLimits(BaseModel):
    """Resource limits for AI worker containers."""

    model_config = ConfigDict(extra="forbid")

    memory_request: str = "512Mi"
    memory_limit: str = "2Gi"
    cpu_request: str = "500m"
    cpu_limit: str = "2000m"


class KubernetesBackendConfig(BaseModel):
    """Kubernetes-specific configuration for AI workers."""

    model_config = ConfigDict(extra="forbid")

    namespace: str = "ml-bot"
    ai_coder_image: str = "ghcr.io/werdnum/ai-coding-base:latest"
    service_account: str = "ai-worker"
    runtime_class: str = "gvisor"
    job_ttl_seconds: int = 3600

    # Secret containing API keys (keys should be ANTHROPIC_API_KEY, GOOGLE_API_KEY, etc.)
    # All keys from this secret are injected as environment variables
    api_keys_secret: str | None = None

    # Optional config volumes for ~/.claude and ~/.gemini
    claude_config_volume: k8s_models.Volume | None = None
    gemini_config_volume: k8s_models.Volume | None = None

    # Resource limits for worker containers
    resources: WorkerResourceLimits = Field(default_factory=WorkerResourceLimits)

    # Name of the PersistentVolumeClaim for workspace storage
    workspace_pvc_name: str = "workspace"

    # Optional explicit kubeconfig path (for local dev; in-cluster config used by default)
    kubeconfig_path: str | None = None

    # Security context for worker pods (None to inherit from container image)
    run_as_user: int | None = 1000
    run_as_group: int | None = 1000
    fs_group: int | None = 1000
    enable_rootless_podman: bool = False

    # Additional volumes and volume mounts to attach to worker pods
    extra_volumes: list[k8s_models.Volume] | None = None
    extra_volume_mounts: list[k8s_models.VolumeMount] | None = None

    # Additional environment variables to inject into worker containers
    extra_env: list[k8s_models.EnvVar] | None = None


class DockerBackendConfig(BaseModel):
    """Docker-specific configuration for AI workers (local development)."""

    model_config = ConfigDict(extra="forbid")

    image: str = "ghcr.io/werdnum/ai-coding-base:latest"
    network: str = "bridge"

    # API keys from host environment variables (names of env vars to pass through)
    # Set to None to disable passing the env var
    anthropic_api_key_env: str | None = "ANTHROPIC_API_KEY"
    gemini_api_key_env: str | None = "GOOGLE_API_KEY"

    # Optional config volume mounts for ~/.claude and ~/.gemini
    claude_config_volume: str | None = None
    gemini_config_volume: str | None = None

    # Resource limits for worker containers
    resources: WorkerResourceLimits = Field(default_factory=WorkerResourceLimits)


class AIWorkerConfig(BaseModel):
    """AI Worker Sandbox configuration.

    Enables spawning isolated AI coding agents (Claude Code or Gemini CLI)
    to handle complex tasks requiring general-purpose computing.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False

    # Backend selection
    backend_type: Literal["kubernetes", "docker", "mock"] = "kubernetes"

    # Volume settings
    workspace_mount_path: str = "/workspace"

    # Webhook URL for worker completion notifications
    # If not set, falls back to server_url + /webhook/event
    # For Kubernetes, use internal service URL like:
    # http://family-assistant.family-assistant.svc.cluster.local:8000/webhook/event
    webhook_url: str | None = None

    # Execution settings
    default_timeout_minutes: int = 30
    max_timeout_minutes: int = 120
    max_concurrent_workers: int = 3

    # Resource limits
    resources: WorkerResourceLimits = Field(default_factory=WorkerResourceLimits)

    # Available AI agent types (used to populate tool enum at runtime)
    available_agents: list[str] = Field(default_factory=lambda: ["claude", "gemini"])

    # Cleanup settings
    task_retention_hours: int = 48

    # Backend-specific configurations
    kubernetes: KubernetesBackendConfig | None = Field(
        default_factory=KubernetesBackendConfig
    )
    docker: DockerBackendConfig | None = Field(default_factory=DockerBackendConfig)


class MQTTConfig(BaseModel):
    """MQTT broker configuration for publishing messages to external devices."""

    model_config = ConfigDict(extra="forbid")

    broker_host: str | None = None
    broker_port: int = 1883
    username: str | None = None
    password: str | None = None


class OIDCConfig(BaseModel):
    """OpenID Connect authentication configuration."""

    model_config = ConfigDict(extra="forbid")

    client_id: str = ""
    client_secret: str = ""
    discovery_url: str = ""
    allowed_emails: list[str] = Field(default_factory=list)


class OpenAIImageRequestConfig(BaseModel):
    """Configuration shared by OpenAI image generate/edit requests."""

    model_config = ConfigDict(extra="forbid")

    size: Literal["1024x1024", "1536x1024", "1024x1536", "auto"] = "auto"
    quality: Literal["low", "medium", "high", "auto"] = "high"
    input_fidelity: Literal["low", "high"] = "high"
    output_format: Literal["png", "jpeg", "webp"] = "png"
    output_compression: int | None = Field(default=None, ge=0, le=100)


class OpenAIImageConfig(BaseModel):
    """OpenAI image configuration for generation and transformation."""

    model_config = ConfigDict(extra="forbid")

    model: str = "gpt-image-2"
    default_generate: OpenAIImageRequestConfig = Field(
        default_factory=OpenAIImageRequestConfig
    )
    default_edit: OpenAIImageRequestConfig = Field(
        default_factory=OpenAIImageRequestConfig
    )


class AppConfig(BaseSettings):
    """Main application configuration.

    This is the root configuration model that contains all application settings.
    Property access is type-safe - misspelled property names will raise AttributeError.

    Inherits from BaseSettings to support layered configuration sources
    (YAML files, env vars) via pydantic-settings.
    """

    model_config = SettingsConfigDict(
        extra="forbid",
        nested_model_default_partial_update=True,
    )

    # ContextVar used to pass YAML file paths to settings_customise_sources thread-safely.
    _yaml_files_ctx: ClassVar[ContextVar[list[str] | None]] = ContextVar(
        "yaml_files", default=None
    )

    @classmethod
    @contextlib.contextmanager
    def yaml_source_context(cls, yaml_files: list[str]) -> Generator[None]:
        """Context manager to set YAML file paths for AppConfig construction.

        YAML paths are normalized to absolute paths against the current cwd
        once, at entry. Downstream consumers — including
        ``_normalize_storage_path`` that resolves relative
        ``attachment_storage_path`` values against the config file's
        directory — can then trust that ``_yaml_files_ctx`` always holds
        absolute paths, regardless of how ``load_config`` was invoked.
        """
        absolute_yaml_files = [os.path.abspath(path) for path in yaml_files]
        token = cls._yaml_files_ctx.set(absolute_yaml_files)
        try:
            yield
        finally:
            cls._yaml_files_ctx.reset(token)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Only use init_settings + optional YAML source. Env vars are handled as post-processing."""
        sources: list[PydanticBaseSettingsSource] = [init_settings]
        yaml_files = cls._yaml_files_ctx.get()
        if yaml_files:
            sources.append(DeepMergedYamlSource(settings_cls, yaml_files))
        return tuple(sources)

    # Secrets and API keys (primarily from environment)
    telegram_token: str | None = None
    telegram_enabled: bool = True
    telegram_api_base_url: str | None = (
        None  # Custom Telegram Bot API URL (for testing or self-hosted)
    )
    openrouter_api_key: str | None = None
    gemini_api_key: str | None = None
    openai_api_key: str | None = None

    # User access control
    users: list[UserIdentityConfig] = Field(default_factory=list)
    allowed_user_ids: list[int] = Field(default_factory=list)
    developer_chat_id: int | None = None

    # Image generation
    image_generation_backend: Literal["openai", "gemini", "mock"] | None = None
    openai_image: OpenAIImageConfig = Field(default_factory=OpenAIImageConfig)

    # Model configuration
    model: str = "gemini/gemini-3.1-pro-preview"
    embedding_model: str = "gemini/gemini-embedding-001"
    embedding_dimensions: int = 1536
    # Optional explicit embedding provider selection. When None, the provider is
    # inferred from the embedding_model name (e.g. "gemini/" prefix, local path).
    # Set to "openai" to use any OpenAI-compatible embeddings endpoint, in which
    # case embedding_model is sent to the API verbatim.
    embedding_provider: Literal["gemini", "openai", "sentence_transformer"] | None = (
        None
    )
    # Base URL for an OpenAI-compatible embeddings endpoint (e.g. OpenRouter:
    # "https://openrouter.ai/api/v1"). Only used when embedding_provider == "openai".
    embedding_base_url: str | None = None
    # API key for the OpenAI-compatible embeddings endpoint. Falls back to
    # openai_api_key / OPENAI_API_KEY when unset.
    embedding_api_key: str | None = None

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
    # Tool policy rules injected into *every* profile's policy engine, regardless
    # of the profile's own tools_policy (which otherwise replaces the defaults
    # wholesale). Use this for tools that must be available in all contexts, such
    # as report_technical_problem. Operator policy can still override these.
    global_tools_policy: ToolPolicyConfig | None = None

    # Feature configurations
    calendar_config: CalendarConfig = Field(default_factory=CalendarConfig)
    pwa_config: PWAConfig = Field(default_factory=PWAConfig)
    apns: ApnsConfig = Field(default_factory=ApnsConfig)
    gemini_live_config: GeminiLiveConfig = Field(default_factory=GeminiLiveConfig)
    mcp_config: MCPConfig = Field(default_factory=MCPConfig)
    indexing_pipeline_config: IndexingPipelineConfig = Field(
        default_factory=IndexingPipelineConfig
    )
    attachment_config: AttachmentConfig = Field(default_factory=AttachmentConfig)
    email_intake: EmailIntakeConfig = Field(default_factory=EmailIntakeConfig)
    event_system: EventSystemConfig = Field(default_factory=EventSystemConfig)
    message_batching_config: MessageBatchingConfig = Field(
        default_factory=MessageBatchingConfig
    )
    ai_worker_config: AIWorkerConfig = Field(default_factory=AIWorkerConfig)
    browser_handoff_config: BrowserHandoffConfig = Field(
        default_factory=BrowserHandoffConfig
    )
    notes_config: NotesConfig = Field(default_factory=NotesConfig)
    skills_config: SkillsConfig = Field(default_factory=SkillsConfig)
    mqtt_config: MQTTConfig = Field(default_factory=MQTTConfig)

    # LLM parameters (pattern -> parameters mapping)
    # ast-grep-ignore: no-dict-any - LLM params are provider-specific and genuinely arbitrary
    llm_parameters: dict[str, dict[str, Any]] = Field(default_factory=dict)

    # Logging configuration
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    # Server port (optional, defaults to 8000)
    server_port: int = 8000

    # Number of in-process TaskWorker instances to run concurrently.
    # Multiple workers are required so a handler that parks waiting on an
    # in-process future (e.g. a confirmation-gated delegated profile run) can be
    # unblocked by a sibling worker servicing the task that resolves it. Workers
    # are in-process only: they share in-memory futures and registries within a
    # single event loop and cannot span processes.
    task_worker_count: int = Field(default=2, ge=1)

    # Attachment selection thresholds (global)
    attachment_selection_threshold: int = 3  # Trigger selection when > this many
    max_response_attachments: int = 6  # Max attachments per response

    @model_validator(mode="after")
    def validate_user_identity_uniqueness(self) -> AppConfig:
        user_ids: set[str] = set()
        oidc_emails: dict[str, str] = {}
        oidc_subjects: dict[str, str] = {}
        telegram_user_ids: dict[int, str] = {}
        email_senders: dict[str, str] = {}
        email_recipients: dict[str, str] = {}

        def add_unique(
            mapping: dict[Any, str],
            value: Any,  # noqa: ANN401 - helper validates heterogeneous identity keys
            user_id: str,
            label: str,
        ) -> None:
            existing_user_id = mapping.get(value)
            if existing_user_id is not None and existing_user_id != user_id:
                msg = (
                    f"{label} {value!r} is configured for both "
                    f"{existing_user_id!r} and {user_id!r}"
                )
                raise ValueError(msg)
            mapping[value] = user_id

        for user in self.users:
            if user.id in user_ids:
                msg = f"Duplicate user id {user.id!r}"
                raise ValueError(msg)
            user_ids.add(user.id)
            for email in user.oidc.emails:
                add_unique(oidc_emails, email, user.id, "OIDC email")
            for subject in user.oidc.subjects:
                add_unique(oidc_subjects, subject, user.id, "OIDC subject")
            for telegram_user_id in user.telegram.user_ids:
                add_unique(
                    telegram_user_ids,
                    telegram_user_id,
                    user.id,
                    "Telegram user id",
                )
            for sender in user.email_intake.sender_addresses:
                add_unique(email_senders, sender, user.id, "Email sender")
            for recipient in user.email_intake.recipient_addresses:
                add_unique(email_recipients, recipient, user.id, "Email recipient")

        return self

    @field_validator("attachment_storage_path", "document_storage_path")
    @classmethod
    def _normalize_storage_path(cls, value: str) -> str:
        """Anchor storage paths to a stable absolute directory at load time.

        Email-attachment ``storage_path`` values are persisted relative to
        ``attachment_storage_path``. If the config value itself were left
        relative, ``AttachmentRegistry`` would resolve it against whatever
        cwd the worker process had at startup — a later restart from a
        different directory would re-anchor the mailbox root and every
        stored relative path would point to the wrong place.

        To make the result stable across restarts regardless of cwd:

        - Absolute values are returned unchanged.
        - Relative values are anchored to the first YAML config file's
          directory (the deployment-owned, restart-invariant location).
          ``settings_customise_sources`` populates
          ``_yaml_files_ctx`` when ``AppConfig`` is constructed via
          ``yaml_source_context`` (production load path).
        - When no YAML context is available (tests, ad-hoc scripts),
          fall back to ``os.path.abspath`` — same cwd-dependent behavior
          as before, but warned about by the caller environment since
          there's no stable anchor to substitute.
        """
        if not value:
            return value
        path = Path(value)
        if path.is_absolute():
            return str(path)
        yaml_files = cls._yaml_files_ctx.get()
        if yaml_files:
            config_dir = Path(yaml_files[0]).resolve().parent
            return str(config_dir / path)
        return os.path.abspath(value)
