"""Configuration for one Home Assistant instance."""

from pydantic import BaseModel, ConfigDict, SecretStr


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
    # Jinja template rendered by Home Assistant into each turn's context. No
    # template means no context provider.
    context_template: str | None = None
    # Run the state-change event source for automations and event listeners.
    events: bool = True
