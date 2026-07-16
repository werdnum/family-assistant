"""Provider-neutral description of an OAuth 2.0 integration.

An :class:`OAuthProviderSpec` carries everything the shared OAuth machinery
(credential resolver, connect-flow router, integration-state evaluation) needs
to serve one provider: its endpoints, scope allowlist, identity scopes, and
provider-specific authorize parameters. Adding a provider means defining one
spec (see ``services/google_provider.py``) — no re-threading of the shared
layers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


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
        authorize_url: OAuth authorization endpoint.
        token_url: OAuth token endpoint.
        revoke_url: OAuth token-revocation endpoint.
        userinfo_url: OpenID Connect userinfo endpoint.
        supported_scopes: Startup allowlist of data scopes an operator may
            configure (narrow-only; unlisted scopes disable the integration).
        identity_scopes: Scopes always appended at authorize so the callback
            can identify the connected account.
        extra_authorize_params: Provider-specific authorize query parameters.
    """

    name: str
    display_name: str
    config_attr: str
    authorize_url: str
    token_url: str
    revoke_url: str
    userinfo_url: str
    supported_scopes: frozenset[str]
    identity_scopes: tuple[str, ...]
    extra_authorize_params: Mapping[str, str]
