"""Configuration for one Home Assistant instance."""

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from family_assistant.plugins.home_assistant.client import (
    DEFAULT_CAMERA_SNAPSHOT_TIMEOUT_SECONDS,
)


class HomeAssistantConfig(BaseModel):
    """Connection and behaviour settings for one Home Assistant server."""

    model_config = ConfigDict(extra="forbid")

    # Optional here because deployments usually supply them through the
    # HOMEASSISTANT_URL and HOMEASSISTANT_API_KEY environment variables, which
    # are applied after the YAML is first validated. Starting an instance
    # without them is an error.
    api_url: str | None = None
    token: SecretStr | None = None
    verify_ssl: bool = True
    # Total time allowed for one camera snapshot. Keep it longer than any
    # gateway in front of the cameras, so the gateway's own error comes back
    # instead of this client giving up first.
    camera_snapshot_timeout_seconds: float = Field(
        default=DEFAULT_CAMERA_SNAPSHOT_TIMEOUT_SECONDS, gt=0
    )
    # Jinja template rendered by Home Assistant into each turn's context. No
    # template means no context provider.
    context_template: str | None = None
    # Run the state-change event source for automations and event listeners.
    events: bool = True
    # Trust this server's state-change event payloads like its output_trusted
    # read tools: event wakes run under the creating profile instead of the
    # restricted event_handler profile, and the payload adds no trigger taint.
    # Off by default, like MCPServerConfig.trusted_events.
    trusted_events: bool = False
