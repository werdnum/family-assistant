"""Connect a sandbox user's Google account to the eval's Google world.

The sandbox keeps the production chain intact: the integration is enabled by
configuration, the user's connection is stored encrypted, the credential
resolver refreshes an access token before every request, and the tools check
their scopes. Only the two network edges are replaced -- the token endpoint
and the data API -- through :class:`ConnectedAccountTransport`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from pydantic import SecretStr

from family_assistant.config_models import GoogleIntegrationConfig
from family_assistant.eval.workhorse.fake_google import FakeGoogleBackend
from family_assistant.services.api_backend import ConnectedAccountTransport
from family_assistant.services.credential_encryption import CredentialEncryption
from family_assistant.services.google_provider import GOOGLE_PROVIDER

if TYPE_CHECKING:
    from datetime import datetime

    from family_assistant.eval.workhorse.google_world import GoogleWorld
    from family_assistant.storage.database import Database

__all__ = [
    "eval_google_integration",
    "google_transport",
    "seed_google_connection",
]

_EVAL_REFRESH_TOKEN = "eval-refresh-token"


def eval_google_integration(
    *, require_taint_enforcement: bool, scopes: list[str] | None = None
) -> GoogleIntegrationConfig:
    """Integration config with throwaway client credentials and a fresh key.

    ``require_taint_enforcement`` has no default: the sandbox must gate the
    tools the way the deployment under test does, and that is the
    deployment's choice, not the harness's.
    """
    return GoogleIntegrationConfig(
        oauth_client_id="eval-client",
        oauth_client_secret=SecretStr("eval-secret"),
        credential_encryption_key=SecretStr(CredentialEncryption.generate_key()),
        scopes=scopes if scopes is not None else GoogleIntegrationConfig().scopes,
        require_taint_enforcement=require_taint_enforcement,
    )


async def seed_google_connection(
    db: Database,
    integration: GoogleIntegrationConfig,
    *,
    user_id: str,
    account_email: str,
) -> None:
    """Store ``user_id``'s Google connection, holding every configured scope."""
    encryption = CredentialEncryption(
        integration.credential_encryption_key.get_secret_value()
    )
    await db.oauth_connections.upsert_connection(
        user_id=user_id,
        provider=GOOGLE_PROVIDER.name,
        provider_account_email=account_email,
        scopes=list(integration.scopes),
        refresh_token_encrypted=encryption.encrypt(_EVAL_REFRESH_TOKEN),
    )


def _token_endpoint(request: httpx.Request) -> httpx.Response:
    form = dict(httpx.QueryParams(request.content.decode("utf-8")))
    if form.get("refresh_token") != _EVAL_REFRESH_TOKEN:
        return httpx.Response(400, json={"error": "invalid_grant"})
    return httpx.Response(
        200,
        json={"access_token": "eval-access-token", "expires_in": 3600},
    )


def google_transport(
    world: GoogleWorld, now: datetime
) -> tuple[ConnectedAccountTransport, FakeGoogleBackend]:
    """The transport to hand the application, and the fake behind it."""
    backend = FakeGoogleBackend(world=world, now=now)
    transport = ConnectedAccountTransport(
        api_backend=backend,
        token_http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(_token_endpoint)
        ),
    )
    return transport, backend
