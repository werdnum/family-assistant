"""Provider-neutral description of an OAuth 2.0 integration.

An :class:`OAuthProviderSpec` carries everything the shared OAuth machinery
(credential resolver, connect-flow router, integration-state evaluation) needs
to serve one provider: its endpoints, scope allowlist, identity scopes, and
provider-specific authorize parameters. Adding a provider means defining one
spec (see ``services/google_provider.py``) — no re-threading of the shared
layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


def default_is_invalid_grant(response: httpx.Response) -> bool:
    """RFC 6749 ``invalid_grant`` classifier for terminal refresh failures."""
    if response.status_code != httpx.codes.BAD_REQUEST:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("error") == "invalid_grant"


@dataclass(frozen=True)
class OAuthProviderSpec:
    """Static description of one OAuth provider.

    Attributes:
        name: Provider key (e.g. ``"google"``) — used as the DB ``provider``
            value, the app.state dict key, the settings-redirect query key, and
            in route names.
        display_name: User-facing provider name (e.g. ``"Google"``) used in
            error and notification text.
        config_attr: Attribute on ``AppConfig`` holding this provider's
            ``OAuthIntegrationConfig``.
        token_url: OAuth token endpoint.
        revoke_url: OAuth token-revocation endpoint.
        supported_scopes: Startup allowlist of data scopes an operator may
            configure (narrow-only; unlisted scopes disable the integration).
        authorize_url: OAuth authorization endpoint (for authorization-code
            providers), or None for device-code-only providers.
        userinfo_url: OpenID Connect or provider userinfo endpoint, if any.
        device_code_url: OAuth device-authorization endpoint (RFC 8628), if any.
        identity_scopes: Scopes always appended at authorize so the callback
            can identify the connected account.
        extra_authorize_params: Provider-specific authorize query parameters.
        requires_client_secret: Whether the provider requires a confidential
            ``oauth_client_secret`` (False for public clients such as Link).
        requires_taint_floor: Whether startup enablement enforces the taint
            policy floor (False when the provider uses static tool-call review
            plus out-of-band approval instead).
        is_terminal_refresh_error: Classifier returning True when a non-200
            token-refresh response means the refresh token is permanently
            invalid/revoked and the connection must flip to ``needs_reauth``.
    """

    name: str
    display_name: str
    config_attr: str
    token_url: str
    revoke_url: str
    supported_scopes: frozenset[str]
    authorize_url: str | None = None
    userinfo_url: str | None = None
    device_code_url: str | None = None
    identity_scopes: tuple[str, ...] = ()
    extra_authorize_params: Mapping[str, str] = field(default_factory=dict)
    requires_client_secret: bool = True
    requires_taint_floor: bool = True
    is_terminal_refresh_error: Callable[[httpx.Response], bool] = (
        default_is_invalid_grant
    )
