"""The adapter's configuration as the endpoint and OAuth routes read it."""

from fastapi import FastAPI

from family_assistant.config_models import AppConfig, MCPAdapterConfig


def adapter_config(app: FastAPI) -> MCPAdapterConfig:
    """The adapter's configuration, read at request time.

    ``create_app`` runs before the Assistant injects ``app.state.config``, so the
    endpoint is always mounted and consults the config per request.
    """
    config: AppConfig | None = getattr(app.state, "config", None)
    if config is None:
        return MCPAdapterConfig()
    return config.mcp_adapter
