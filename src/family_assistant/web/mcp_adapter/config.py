"""The adapter's configuration as the endpoint and OAuth routes read it."""

from fastapi import FastAPI

from family_assistant.config_models import AppConfig, MCPAdapterConfig
from family_assistant.web import auth, jwt_tokens


def adapter_config(app: FastAPI) -> MCPAdapterConfig:
    """The adapter's configuration, read at request time.

    ``create_app`` runs before the Assistant injects ``app.state.config``, so the
    endpoint is always mounted and consults the config per request.
    """
    config: AppConfig | None = getattr(app.state, "config", None)
    if config is None:
        return MCPAdapterConfig()
    return config.mcp_adapter


def require_authentication_for_adapter(
    config: AppConfig,
    *,
    oidc_enabled: bool | None = None,
    jwt_enabled: bool | None = None,
) -> None:
    """Refuse to start with the adapter on and no authentication configured.

    Without OIDC or a signed-JWT key the application serves every request as a
    synthetic development user. The rest of the API shares that posture, but the
    adapter exists to be reached from the public internet by claude.ai, so an
    installation that enables it without a way to authenticate the caller fails
    at startup rather than answering anyone who finds the endpoint.
    """
    if not config.mcp_adapter.enabled:
        return
    if oidc_enabled is None:
        oidc_enabled = auth.AUTH_ENABLED
    if jwt_enabled is None:
        jwt_enabled = jwt_tokens.JWTTokenService.from_environment().enabled
    if oidc_enabled or jwt_enabled:
        return
    raise ValueError(
        "mcp_adapter.enabled requires authentication: configure OIDC "
        "(OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, OIDC_DISCOVERY_URL, SESSION_SECRET_KEY) "
        "or signed-JWT API tokens (JWT_SIGNING_KEY), or disable the adapter."
    )
