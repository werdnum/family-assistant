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

Fields that hold credentials
----------------------------
Type a credential-bearing field ``SecretStr``. Pydantic then masks it in
``model_dump``, so it cannot reach a diagnostic dump
(``get_resolved_config``, ``get_profile_config``, ``GET /api/debug/profiles``)
or a config log line, and the guarantee is enforced by the type rather than by
anything guessing from the field's name. Read the value with
``.get_secret_value()`` at the point of use; the type checker will point out
every place that needs it.

Two things this cannot express, handled in
:mod:`family_assistant.config_inspection` instead:

* A credential *inside* a larger value -- the password in ``database_url``, a
  ``token=`` parameter in an endpoint. The field as a whole is not secret and
  masking it would throw away the host and database an operator needs.
* Config whose shape is not declared -- ``mcp_config.mcpServers`` uses
  ``extra="allow"`` and its ``env`` blocks are keyed by operator-chosen
  variable names, so there is no field to annotate.

Add a case to ``tests/unit/test_config_inspection.py`` when you add a
credential field.
"""

from __future__ import annotations

import contextlib
import os
import zoneinfo
from contextvars import ContextVar
from email.utils import parseaddr
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

import cloudcoil.models.kubernetes.core.v1 as k8s_models  # noqa: TC002 - Pydantic needs at runtime
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from collections.abc import Generator

    from pydantic_settings import PydanticBaseSettingsSource

from .config_sources import DeepMergedYamlSource
from .delegation_security import DelegationSecurityLevel
from .security.taint import SinkClass, TaintPolicyConfig
from .tools.policy import (
    ToolPolicyConfig,
    ToolPolicyDecision,
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


class ToolCallReviewEscalationConfig(BaseModel):
    """Turn-local thresholds used by tool-call review escalation."""

    model_config = ConfigDict(extra="forbid")

    consecutive_denials: int = Field(default=3, ge=1)
    total_denials_per_turn: int = Field(default=20, ge=1)


class ToolCallReviewConfig(BaseModel):
    """Configuration for the non-agentic tool-call reviewer."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    provider: str | None = "google"
    # See defaults.yaml: 3.8 roughly sextuples benign friction on real household
    # tool calls without allowing fewer attacks, so the judge stays on 3.7.
    model: str = "gemini-3.7-flash"
    retry_config: RetryConfig | None = None
    timeout_seconds: float = Field(default=30.0, gt=0)
    max_reviews_per_turn: int = Field(default=25, ge=1)
    escalation: ToolCallReviewEscalationConfig = Field(
        default_factory=ToolCallReviewEscalationConfig
    )
    guidance: str = ""


class ReolinkCameraItemConfig(BaseModel):
    """Configuration for a single Reolink camera."""

    model_config = ConfigDict(extra="forbid")

    host: str
    username: str
    password: SecretStr
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


class AntigravityEgressCredentialConfig(BaseModel):
    """A credential the sandbox's egress proxy injects on matching requests.

    Names a *kind* of credential, never a value: the secret material is read
    from the process environment when a run is submitted, so a leaked config
    file discloses which domains get a credential rather than the credential.
    The sandbox never receives it either -- the proxy adds the header on the
    way out, so nothing the agent can print or write to a file contains it.
    """

    model_config = ConfigDict(extra="forbid")

    # `github_app`: mint a short-lived installation access token from the
    # GitHub App named by GITHUB_APP_ID / GITHUB_APP_INSTALLATION_ID and the
    # private key at GITHUB_APP_PRIVATE_KEY_PATH (or inline in
    # GITHUB_APP_PRIVATE_KEY). `bearer`: use the static token in `token_env`.
    type: Literal["github_app", "bearer"]
    header_name: str = "Authorization"
    # `bearer` renders "Bearer <token>"; `basic` renders
    # "Basic base64(x-access-token:<token>)", which is how GitHub authenticates
    # git-over-HTTPS as opposed to its REST API. Getting this wrong surfaces as
    # a 401 midway through an agent run rather than as a config error, so it is
    # chosen per rule rather than guessed from the domain.
    scheme: Literal["bearer", "basic"] = "bearer"
    # Required by (and only meaningful to) `type: "bearer"`.
    token_env: str | None = None

    @model_validator(mode="after")
    def validate_credential(self) -> AntigravityEgressCredentialConfig:
        if self.type == "bearer" and not self.token_env:
            msg = "Antigravity egress credential of type 'bearer' requires 'token_env'"
            raise ValueError(msg)
        if self.type != "bearer" and self.token_env:
            msg = (
                f"Antigravity egress credential of type '{self.type}' does not "
                "read 'token_env'"
            )
            raise ValueError(msg)
        return self


class AntigravityEgressRuleConfig(BaseModel):
    """One domain rule for the Antigravity sandbox's egress proxy."""

    model_config = ConfigDict(extra="forbid")

    # Supports wildcards ("*.githubusercontent.com"); "*" matches every domain,
    # which is how the API spells "restrict nothing, but still inject headers
    # on the rules that carry a credential".
    domain: str
    # Static headers injected alongside any credential. For non-secret values
    # only -- a secret belongs in `credential`, which reads the environment.
    headers: dict[str, str] = Field(default_factory=dict)
    credential: AntigravityEgressCredentialConfig | None = None


class AntigravityEnvironmentConfig(BaseModel):
    """The sandbox environment one Antigravity run gets.

    Today this is the egress policy: whether the sandbox reaches the network at
    all, which domains it may reach, and which credentials the proxy attaches
    on the way out. Mounted files are not configured here -- they come from a
    delegation's attachments (see ``InteractionsAgentProcessingService``).
    """

    model_config = ConfigDict(extra="forbid")

    # `default` sends no network block, leaving the API's own policy (all
    # outbound traffic allowed, no injection). `disabled` cuts the sandbox off
    # entirely. `allowlist` sends `allowlist` and nothing else is reachable.
    network: Literal["default", "disabled", "allowlist"] = "default"
    allowlist: list[AntigravityEgressRuleConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_network(self) -> AntigravityEnvironmentConfig:
        if self.network == "allowlist" and not self.allowlist:
            msg = (
                "Antigravity environment sets network: 'allowlist' with an "
                "empty allowlist, which leaves the sandbox unable to reach "
                "anything. Use network: 'disabled' to mean that deliberately."
            )
            raise ValueError(msg)
        if self.network != "allowlist" and self.allowlist:
            msg = (
                f"Antigravity environment sets an allowlist but network is "
                f"'{self.network}', so the allowlist would be discarded. Set "
                "network: 'allowlist' to apply it."
            )
            raise ValueError(msg)
        return self


class AntigravityConfig(BaseModel):
    """Runtime configuration for a Google Antigravity managed-agent profile.

    Only meaningful on a profile whose ``llm_model`` is the Antigravity agent
    id: the agent id selects the managed agent, and these fields select the
    model it reasons with, cap what a single run may spend, and describe the
    sandbox environment it runs in.
    """

    model_config = ConfigDict(extra="forbid")

    # The agent's reasoning model. Pinned rather than left to the API default
    # so an upstream default change is a config change here, not a silent
    # behaviour change in a profile users have calibrated their prompts to.
    model: str = "gemini-3.8-flash"
    # Ceiling on the tokens one agent run may consume. Unset means the API's
    # own default; the agent plans and executes autonomously in a sandbox, so
    # this is the only bound on how long it iterates other than wall clock.
    max_total_tokens: int | None = Field(default=None, gt=0)
    # Unset means a fresh default sandbox with unrestricted egress and no
    # credentials, which is what the profile ships as. Configuring a credential
    # here widens the profile's Rule of Two class -- see
    # docs/design/antigravity-environment-and-credentials.md.
    environment: AntigravityEnvironmentConfig | None = None


# The `name` of every context provider the assistant can attach to a profile.
# Duplicated here rather than imported so config validation does not depend on
# the provider module; `test_context_provider_names_match_config` keeps the two
# in step, since a name drifting out of this set would silently turn an
# exclusion into a no-op.
CONTEXT_PROVIDER_NAMES: frozenset[str] = frozenset({
    "notes",
    "calendar",
    "known_users",
    "weather",
    "home_assistant",
})

_GLOB_METACHARACTERS = "*?["


def _is_glob(name: str) -> bool:
    """Whether a tool name contains `fnmatch` wildcard syntax."""
    return any(char in name for char in _GLOB_METACHARACTERS)


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
    review_guidance: str = ""
    delegation_security_level: DelegationSecurityLevel = DelegationSecurityLevel.CONFIRM
    allowed_delegation_sources: list[str] | None = None
    home_assistant_api_url: str | None = None
    home_assistant_token: SecretStr | None = None
    home_assistant_context_template: str | None = None
    home_assistant_verify_ssl: bool = True
    include_system_docs: list[str] | None = None
    # Context providers inject the user's own data -- notes, calendar, known
    # users, weather, Home Assistant state -- into the system prompt. A profile
    # that exists to look at one attachment and answer in text has no use for
    # any of it, and injecting it hands private data to a prompt built around
    # untrusted content. Listing a provider name here drops it for this profile.
    excluded_context_providers: list[str] = Field(default_factory=list)
    # Master switch for the same data, above excluded_context_providers: false
    # means the profile receives no aggregated context at all. Defaults to false
    # so a profile nobody thought about is denied rather than granted -- most
    # shipped profiles want none of it, and two of them (media_analyst,
    # telephone_external) must not have it. The current time is injected either
    # way; it is not what this gates.
    include_aggregated_context: bool = False

    @field_validator("excluded_context_providers")
    @classmethod
    def validate_excluded_context_providers(cls, v: list[str]) -> list[str]:
        unknown = sorted(set(v) - CONTEXT_PROVIDER_NAMES)
        if unknown:
            msg = (
                f"Unknown context provider(s) in excluded_context_providers: "
                f"{', '.join(unknown)}. Valid names: "
                f"{', '.join(sorted(CONTEXT_PROVIDER_NAMES))}."
            )
            raise ValueError(msg)
        return v

    # Only read when llm_model is the Antigravity managed agent; a profile
    # pointing anywhere else is rejected at startup rather than silently
    # ignoring this.
    antigravity_config: AntigravityConfig | None = None
    # The runtime-taint sink class a whole turn on this profile counts as.
    # Set it on a profile whose turn is itself a privileged operation -- a
    # sandbox that runs code, say -- so the taint matrix gates reaching the
    # profile at all, the way it already gates the equivalent tool. Unset (the
    # default) means the profile is not a sink in its own right and only its
    # tools are evaluated.
    taint_sink_class: SinkClass | None = None

    max_iterations: int = 5
    context_pruning_min_turns: int = 3
    calendar_config: CalendarConfig | None = None  # Per-profile calendar config
    camera_config: CameraConfig | None = None  # Per-profile camera backend config
    greeting_wav_path: str | None = None
    default_note_visibility_labels: list[str] | None = None
    required_note_visibility_labels: list[str] | None = None
    allowed_note_visibility_labels: list[str] | None = None
    allow_wake_llm: bool = True
    enable_computer_use: bool = False
    computer_use_excluded_functions: list[str] = Field(default_factory=list)
    # Submit-then-poll tuning for a pollable local profile (e.g. Deep
    # Research) delegated to via delegate_to_service. Ignored by ordinary
    # local profiles, which are never pollable. Mirrors RemoteA2AConfig's
    # fields of the same name; unset means "use the worker's module defaults".
    poll_interval_seconds: float | None = Field(default=None, gt=0)
    max_async_seconds: float | None = Field(default=None, gt=0)


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
    async_delegation_enabled: bool = True
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


# Default wall-clock cap for an async (submit-then-poll) A2A delegation when
# ``max_async_seconds`` is not set explicitly. This is intentionally decoupled
# from ``timeout_seconds`` (the per-HTTP-call timeout): an async delegation is
# polled across many short HTTP calls and may legitimately run far longer than a
# single request. The cap only exists to reap a genuinely orphaned remote run
# that never reaches a terminal state; the assistant can poll a still-running
# delegation via ``get_delegation_status`` within this envelope.
DEFAULT_REMOTE_MAX_ASYNC_SECONDS = 3600.0


class RemoteA2AConfig(BaseModel):
    """Configuration for a remote A2A agent profile."""

    model_config = ConfigDict(extra="forbid")

    agent_url: str
    auth: RemoteA2AAuthConfig = Field(default_factory=RemoteA2AAuthConfig)
    timeout_seconds: float = Field(default=300.0, gt=0)
    skills_description: str | None = None
    # Async (submit-then-poll) delegation tuning. poll_interval_seconds is the
    # base cadence for polling an in-flight remote task; max_async_seconds is the
    # total wall-clock cap before the delegation is cancelled + failed. When
    # max_async_seconds is unset it defaults to DEFAULT_REMOTE_MAX_ASYNC_SECONDS
    # (1 hour) rather than the per-call timeout, so a long-running remote run is
    # not killed at the 5-minute HTTP-call boundary.
    poll_interval_seconds: float = Field(default=10.0, gt=0)
    max_async_seconds: float | None = Field(default=None, gt=0)


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
    taint_policy: TaintPolicyConfig | None = None
    operator_tools_policy: ToolPolicyConfig | None = Field(default=None, exclude=True)
    chat_id_to_name_map: dict[int, str] = Field(default_factory=dict)
    slash_commands: list[str] = Field(default_factory=list)
    visibility_grants: list[str] = Field(default_factory=list)
    # Tool names to withhold from this profile even though `global_tools_policy`
    # grants them to every profile. A profile's own `tools_policy` cannot deny a
    # global grant -- global rules are injected at the `profile` policy layer,
    # which outranks the `defaults` layer a profile's own policy occupies, so
    # layer beats priority. This is the only way for a profile that must hold no
    # privileges to actually hold none.
    excluded_global_tools: list[str] = Field(default_factory=list)
    remote_a2a: RemoteA2AConfig | None = None


class DefaultProfileSettings(BaseModel):
    """Default settings applied to all profiles unless overridden."""

    model_config = ConfigDict(extra="forbid")

    processing_config: ProcessingConfig = Field(default_factory=ProcessingConfig)
    tools_config: ToolsConfig = Field(default_factory=ToolsConfig)
    tools_policy: ToolPolicyConfig | None = None
    taint_policy: TaintPolicyConfig | None = None
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
    password: SecretStr | None = None
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
    vapid_private_key: SecretStr | None = None
    vapid_contact_email: str | None = None


class ApnsConfig(BaseModel):
    """Apple Push Notification service (iOS) configuration.

    The APNs sender is enabled only when team_id, key_id, bundle_id and a private key (either
    `auth_key` inline or `auth_key_path`) are all configured.
    """

    model_config = ConfigDict(extra="forbid")

    team_id: str | None = None
    key_id: str | None = None
    auth_key: SecretStr | None = None
    auth_key_path: str | None = None
    bundle_id: str | None = None
    use_sandbox: bool = False


class OAuthIntegrationConfig(BaseModel):
    """Per-user OAuth integration configuration for one provider.

    OAuth client credentials, the Fernet key used to encrypt stored refresh
    tokens at rest, the operator-tunable data scopes requested at consent, and
    whether the provider's data tools require taint enforcement before they
    register.
    """

    model_config = ConfigDict(extra="forbid")

    oauth_client_id: str = ""
    oauth_client_secret: SecretStr = SecretStr("")
    credential_encryption_key: SecretStr = SecretStr("")
    scopes: list[str] = Field(default_factory=list)
    require_taint_enforcement: bool = True


class GoogleIntegrationConfig(OAuthIntegrationConfig):
    """Per-user Gmail/Drive integration configuration.

    Enables the user-scoped Google data feature: OAuth client credentials, the
    Fernet key used to encrypt stored refresh tokens at rest, the operator-tunable
    data scopes requested at consent, and whether the Gmail/Drive tools require
    taint enforcement before they register.
    """

    scopes: list[str] = Field(
        default_factory=lambda: [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.compose",
            "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/drive.file",
        ]
    )


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

    mailgun_webhook_signing_key: SecretStr | None = None
    mailgun_signature_max_age_seconds: int = Field(default=300, gt=0)
    allowed_sender_addresses: list[str] = Field(default_factory=list)
    allowed_recipient_addresses: list[str] = Field(default_factory=list)
    known_contact_sender_addresses: list[str] = Field(default_factory=list)
    recognized_machine_sender_addresses: list[str] = Field(default_factory=list)
    require_authenticated_sender: bool = False
    require_user_mapping: bool = False
    enable_actions: bool = False
    action_profile_id: str = "email_intake"
    user_mappings: list[EmailIntakeUserMapping] = Field(default_factory=list)
    max_raw_request_bytes: int = Field(default=25 * 1024 * 1024, gt=0)
    max_attachment_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    max_total_attachment_bytes: int = Field(default=25 * 1024 * 1024, gt=0)
    outbound_mailgun_api_key: SecretStr | None = None
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
    secrets: dict[str, SecretStr] = Field(
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


class KeychuteConfig(BaseModel):
    """Configuration for brokered HTTP calls from scripts."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    url: str | None = None
    token: SecretStr | None = None
    token_file: str | None = None
    ca_bundle: str | None = None
    max_response_bytes: int = Field(default=25 * 1024 * 1024, ge=1)


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
    # Declared (rather than left to extra="allow") so diagnostic dumps mask it
    # by type. `env` values cannot be declared this way -- their keys are
    # operator-chosen environment variable names -- so they are redacted
    # structurally by config_inspection instead.
    token: SecretStr | None = None


class MCPConfig(BaseModel):
    """MCP (Model Context Protocol) servers configuration."""

    model_config = ConfigDict(extra="forbid")

    mcpServers: dict[str, MCPServerConfig] = Field(default_factory=dict)


def mcp_servers_for_runtime(
    mcp_config: MCPConfig,
    # ast-grep-ignore: no-dict-any - Runtime MCP server dicts are heterogeneous by design
) -> dict[str, dict[str, Any]]:
    """Serialize MCP server entries for connecting, with the token unwrapped.

    ``model_dump`` masks the ``SecretStr`` token, which is what diagnostic
    dumps want and what a client trying to authenticate must not get.
    """
    # ast-grep-ignore: no-dict-any - Runtime MCP server dicts are heterogeneous by design
    servers: dict[str, dict[str, Any]] = {}
    for server_id, server_config in mcp_config.mcpServers.items():
        dumped = server_config.model_dump()
        token = server_config.token
        if token is not None:
            dumped["token"] = token.get_secret_value()
        servers[server_id] = dumped
    return servers


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
    password: SecretStr | None = None


class UCPConfigObject(BaseModel):
    """UCP extension object whose schema is defined by a service or capability."""

    model_config = ConfigDict(extra="allow")


class UCPAvailableInstrumentConfig(BaseModel):
    """Payment instrument advertised by a UCP payment handler."""

    model_config = ConfigDict(extra="allow")

    type: str
    constraints: UCPConfigObject | None = None


class UCPServiceBindingConfig(BaseModel):
    """UCP service transport binding advertised by this platform."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    version: str = "2026-04-08"
    spec: str = "https://ucp.dev/2026-04-08/specification/overview"
    transport: Literal["rest", "mcp", "a2a", "embedded"] = "mcp"
    schema_url: str | None = Field(
        default="https://ucp.dev/2026-04-08/services/shopping/mcp.openrpc.json",
        alias="schema",
    )
    endpoint: str | None = None
    id: str | None = None
    config: UCPConfigObject | None = None


class UCPCapabilityConfig(BaseModel):
    """UCP capability declaration advertised by this platform."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    version: str = "2026-04-08"
    spec: str
    schema_url: str = Field(alias="schema")
    id: str | None = None
    config: UCPConfigObject | None = None
    extends: str | list[str] | None = None


class UCPPaymentHandlerConfig(BaseModel):
    """UCP payment handler declaration advertised by this platform."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    version: str
    spec: str
    schema_url: str = Field(alias="schema")
    available_instruments: list[UCPAvailableInstrumentConfig] | None = None
    config: UCPConfigObject | None = None


class UCPSigningKeyConfig(BaseModel):
    """Public signing key material to publish in the UCP profile."""

    model_config = ConfigDict(extra="forbid")

    kid: str
    kty: str = "EC"
    crv: str
    x: str
    y: str
    use: str = "sig"
    alg: str


class UCPConfig(BaseModel):
    """Universal Commerce Protocol platform profile and signing configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    version: str = "2026-04-08"
    profile_path: str = "/.well-known/ucp"
    profile_url: str | None = None
    profile_cache_max_age_seconds: int = Field(default=300, ge=60)
    signing_key_id: str | None = None
    signing_private_key: SecretStr | None = None
    signing_private_key_path: str | None = None
    additional_signing_keys: list[UCPSigningKeyConfig] = Field(default_factory=list)
    trusted_endpoint_suffixes: list[str] = Field(
        default_factory=lambda: ["myshopify.com"]
    )
    """Host suffixes a merchant profile may point its shopping MCP endpoint at
    even when cross-site, letting Shopify storefronts on custom domains resolve
    to their ``*.myshopify.com`` shop host. Same-origin and same-site endpoints
    are always accepted regardless of this list."""
    services: dict[str, list[UCPServiceBindingConfig]] = Field(
        default_factory=lambda: {
            "dev.ucp.shopping": [UCPServiceBindingConfig()],
        }
    )
    capabilities: dict[str, list[UCPCapabilityConfig]] = Field(
        default_factory=lambda: {
            "dev.ucp.shopping.checkout": [
                UCPCapabilityConfig(
                    spec="https://ucp.dev/2026-04-08/specification/checkout",
                    schema="https://ucp.dev/2026-04-08/schemas/shopping/checkout.json",
                )
            ],
            "dev.ucp.shopping.cart": [
                UCPCapabilityConfig(
                    spec="https://ucp.dev/2026-04-08/specification/cart",
                    schema="https://ucp.dev/2026-04-08/schemas/shopping/cart.json",
                )
            ],
            "dev.ucp.shopping.fulfillment": [
                UCPCapabilityConfig(
                    spec="https://ucp.dev/2026-04-08/specification/fulfillment",
                    schema="https://ucp.dev/2026-04-08/schemas/shopping/fulfillment.json",
                    extends="dev.ucp.shopping.checkout",
                )
            ],
            "dev.ucp.shopping.order": [
                UCPCapabilityConfig(
                    spec="https://ucp.dev/2026-04-08/specification/order",
                    schema="https://ucp.dev/2026-04-08/schemas/shopping/order.json",
                )
            ],
            "dev.ucp.common.identity_linking": [
                UCPCapabilityConfig(
                    spec="https://ucp.dev/2026-04-08/specification/identity-linking",
                    schema="https://ucp.dev/2026-04-08/schemas/common/identity_linking.json",
                )
            ],
        }
    )
    payment_handlers: dict[str, list[UCPPaymentHandlerConfig]] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def require_served_or_absolute_profile_url(self) -> UCPConfig:
        if self.profile_url is None and self.profile_path != "/.well-known/ucp":
            msg = (
                "ucp_config.profile_url is required when ucp_config.profile_path "
                "is not /.well-known/ucp"
            )
            raise ValueError(msg)
        return self


class OIDCConfig(BaseModel):
    """OpenID Connect authentication configuration."""

    model_config = ConfigDict(extra="forbid")

    client_id: str = ""
    client_secret: SecretStr = SecretStr("")
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


class GeminiImageConfig(BaseModel):
    """Gemini image configuration for generation and transformation.

    ``model`` selects the Gemini image model. Examples: ``gemini-3-pro-image``
    (highest quality, the default), ``gemini-3.1-flash-image`` (Nano Banana 2,
    high-efficiency) and ``gemini-3.1-flash-lite-image`` (Nano Banana Lite,
    fastest and cheapest).
    """

    model_config = ConfigDict(extra="forbid")

    model: str = "gemini-3-pro-image"


class VeoVideoConfig(BaseModel):
    """Veo video-generation configuration (long-running ``generateVideos`` API)."""

    model_config = ConfigDict(extra="forbid")

    model: str = "veo-3.1-generate-preview"


class GeminiOmniVideoConfig(BaseModel):
    """Gemini Omni Flash video-generation configuration (Interactions API)."""

    model_config = ConfigDict(extra="forbid")

    model: str = "gemini-omni-flash-preview"


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
    telegram_token: SecretStr | None = None
    telegram_enabled: bool = True
    telegram_api_base_url: str | None = (
        None  # Custom Telegram Bot API URL (for testing or self-hosted)
    )
    openrouter_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None

    # User access control
    users: list[UserIdentityConfig] = Field(default_factory=list)
    allowed_user_ids: list[int] = Field(default_factory=list)
    developer_chat_id: int | None = None

    # Image generation
    image_generation_backend: Literal["openai", "gemini", "mock"] | None = None
    openai_image: OpenAIImageConfig = Field(default_factory=OpenAIImageConfig)
    gemini_image: GeminiImageConfig = Field(default_factory=GeminiImageConfig)

    # Video generation. When backend is None it is inferred from the requested
    # model (``veo-*`` -> Veo, otherwise Gemini Omni Flash), defaulting to
    # Gemini Omni Flash.
    video_generation_backend: Literal["veo", "gemini_omni", "mock"] | None = None
    veo_video: VeoVideoConfig = Field(default_factory=VeoVideoConfig)
    gemini_omni_video: GeminiOmniVideoConfig = Field(
        default_factory=GeminiOmniVideoConfig
    )

    # Model configuration
    model: str = "gemini/gemini-3.8-flash"
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
    embedding_api_key: SecretStr | None = None

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
    willyweather_api_key: SecretStr | None = None
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
    taint_policy: TaintPolicyConfig = Field(default_factory=TaintPolicyConfig)
    tool_call_review: ToolCallReviewConfig | None = None

    # Feature configurations
    calendar_config: CalendarConfig = Field(default_factory=CalendarConfig)
    pwa_config: PWAConfig = Field(default_factory=PWAConfig)
    apns: ApnsConfig = Field(default_factory=ApnsConfig)
    google_integration: GoogleIntegrationConfig = Field(
        default_factory=GoogleIntegrationConfig
    )
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
    keychute_config: KeychuteConfig = Field(default_factory=KeychuteConfig)
    ai_worker_config: AIWorkerConfig = Field(default_factory=AIWorkerConfig)
    browser_handoff_config: BrowserHandoffConfig = Field(
        default_factory=BrowserHandoffConfig
    )
    notes_config: NotesConfig = Field(default_factory=NotesConfig)
    skills_config: SkillsConfig = Field(default_factory=SkillsConfig)
    mqtt_config: MQTTConfig = Field(default_factory=MQTTConfig)
    ucp_config: UCPConfig = Field(default_factory=UCPConfig)

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
    def validate_excluded_global_tools_are_granted(self) -> AppConfig:
        """Reject an exclusion that withholds nothing.

        `excluded_global_tools` exists to take a globally granted tool away from a
        profile that must hold no privileges. A name that no global rule grants
        withholds nothing — whether it is a typo or a tool that was never global —
        and the generated matcher silently matches nothing, leaving the grant it
        was meant to remove in place. For a profile processing untrusted input
        that is a security control that reads as configured and does nothing, so
        it fails at startup instead.

        Only enforced against name-based global rules that can actually confer
        access. A rule matching by tag or MCP server could still grant the named
        tool, and this cannot tell, so a policy containing one of those is left
        alone rather than risking a false rejection. A `deny` rule is the reverse
        error: counting the names it denies as granted would let an exclusion
        matching only that deny validate while withholding nothing, which is the
        no-op this exists to reject. `confirm` counts as granted, since a tool
        reachable behind a confirmation is still reachable.

        Names are glob patterns on both sides, because `ToolMatcher.matches`
        resolves them with `fnmatchcase`. Comparing them as literals would reject
        a working config -- a grant of `read_*` excluded as `read_text_attachment`,
        or the reverse -- and refusing to start is a worse failure than the no-op
        this guards against. When both sides are patterns, neither matches the
        other's literal text even where their match sets overlap (`read_*` and
        `*_attachment` meet on `read_text_attachment`), so such a pair is left
        alone: deciding whether two globs can intersect is not worth doing to
        reach a stricter answer than "cannot tell".
        """
        if self.global_tools_policy is None:
            granted: set[str] = set()
            has_unanalysable_rule = False
        else:
            granting_rules = [
                rule
                for rule in self.global_tools_policy.rules
                if rule.decision is not ToolPolicyDecision.DENY
            ]
            granted = {
                name for rule in granting_rules for name in (rule.match.names or ())
            }
            has_unanalysable_rule = any(
                rule.match.names is None for rule in granting_rules
            )
        if has_unanalysable_rule:
            return self

        for profile in self.service_profiles:
            ineffective = sorted(
                excluded
                for excluded in set(profile.excluded_global_tools)
                # Either direction counts: the exclusion may be the pattern and
                # the grant concrete, or the other way round. Overlap in either
                # sense means the two rules can meet on a real tool.
                if not any(
                    fnmatchcase(grant, excluded)
                    or fnmatchcase(excluded, grant)
                    or (_is_glob(grant) and _is_glob(excluded))
                    for grant in granted
                )
            )
            if ineffective:
                msg = (
                    f"Profile {profile.id!r} excludes global tool(s) "
                    f"{', '.join(ineffective)}, which global_tools_policy does not "
                    "grant, so the exclusion withholds nothing. Remove the entry, "
                    "or correct it to one of: "
                    f"{', '.join(sorted(granted)) or '(none granted)'}."
                )
                raise ValueError(msg)
        return self

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
