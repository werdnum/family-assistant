"""The OAuth endpoints, gated on ``mcp_adapter.enabled`` and bound to ``server_url``.

The SDK's ``create_auth_routes`` bakes the issuer URL into the metadata document
when it builds the routes, and ``create_app`` runs before ``app.state.config``
exists. Rather than re-implement the handlers with per-request metadata, the
routes are registered on the FastAPI app as thin dispatchers that build the
SDK's route table on first use and keep it on ``app.state``, rebuilding it if
``server_url`` changes. Each dispatcher reads its application from the ASGI
scope, so the same route objects serve whichever app they are attached to.
"""

import logging
from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mcp.server.auth.handlers.metadata import ProtectedResourceMetadataHandler
from mcp.server.auth.routes import create_auth_routes
from mcp.shared.auth import ProtectedResourceMetadata
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.routing import Route, Router
from starlette.types import Receive, Scope, Send

from family_assistant.web.auth import MCP_ENDPOINT_PATH
from family_assistant.web.mcp_adapter.config import adapter_config
from family_assistant.web.mcp_adapter.oauth.provider import (
    ASSISTANT_SCOPE,
    CLIENT_REGISTRATION_OPTIONS,
    REVOCATION_OPTIONS,
    FamilyAssistantOAuthProvider,
)

logger = logging.getLogger(__name__)

AUTHORIZATION_SERVER_METADATA_PATH = "/.well-known/oauth-authorization-server"
PROTECTED_RESOURCE_METADATA_PATH = (
    "/.well-known/oauth-protected-resource" + MCP_ENDPOINT_PATH
)

# Path -> methods, as the SDK registers them (OPTIONS for its CORS preflight).
AUTH_SERVER_ROUTES: dict[str, list[str]] = {
    AUTHORIZATION_SERVER_METADATA_PATH: ["GET", "OPTIONS"],
    "/authorize": ["GET", "POST"],
    "/token": ["POST", "OPTIONS"],
    "/register": ["POST", "OPTIONS"],
    "/revoke": ["POST", "OPTIONS"],
}


@dataclass
class OAuthServer:
    """One application's authorization server: its provider and SDK routes."""

    issuer_url: str
    provider: FamilyAssistantOAuthProvider
    router: Router
    protected_resource: ProtectedResourceMetadataHandler


def _issuer_url(app: FastAPI) -> str:
    return str(app.state.config.server_url).rstrip("/")


def oauth_server(app: FastAPI) -> OAuthServer:
    """The authorization server for ``app``, built on first use.

    The provider persists across rebuilds so a ``server_url`` change mid-flow
    does not drop pending consents.
    """
    issuer_url = _issuer_url(app)
    existing: OAuthServer | None = getattr(app.state, "mcp_oauth_server", None)
    if existing is not None and existing.issuer_url == issuer_url:
        return existing

    provider = (
        existing.provider
        if existing is not None
        else FamilyAssistantOAuthProvider(lambda: app.state.database_engine)
    )
    issuer = AnyHttpUrl(issuer_url)
    server = OAuthServer(
        issuer_url=issuer_url,
        provider=provider,
        router=Router(
            routes=create_auth_routes(
                provider,
                issuer_url=issuer,
                client_registration_options=CLIENT_REGISTRATION_OPTIONS,
                revocation_options=REVOCATION_OPTIONS,
            )
        ),
        protected_resource=ProtectedResourceMetadataHandler(
            ProtectedResourceMetadata(
                resource=AnyHttpUrl(issuer_url + MCP_ENDPOINT_PATH),
                authorization_servers=[issuer],
                scopes_supported=[ASSISTANT_SCOPE],
                resource_name="Family Assistant",
            )
        ),
    )
    app.state.mcp_oauth_server = server
    logger.debug("MCP OAuth authorization server built for issuer %s", issuer_url)
    return server


async def _not_enabled(scope: Scope, receive: Receive, send: Send) -> None:
    response = JSONResponse(
        status_code=404, content={"detail": "MCP adapter is not enabled."}
    )
    await response(scope, receive, send)


class _AuthServerDispatch:
    """ASGI endpoint: 404 while disabled, else the SDK's router for this app."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        app: FastAPI = scope["app"]
        if not adapter_config(app).enabled:
            await _not_enabled(scope, receive, send)
            return
        await oauth_server(app).router(scope, receive, send)


class _ProtectedResourceDispatch:
    """ASGI endpoint for the RFC 9728 metadata that names the issuer."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        app: FastAPI = scope["app"]
        if not adapter_config(app).enabled:
            await _not_enabled(scope, receive, send)
            return
        handler = oauth_server(app).protected_resource
        response = await handler.handle(Request(scope, receive))
        await response(scope, receive, send)


def oauth_routes() -> list[Route]:
    """Starlette routes for the authorization server and resource metadata.

    A class instance as ``endpoint`` is treated by Starlette as an ASGI app,
    which is what lets the dispatchers see the raw scope.
    """
    dispatch = _AuthServerDispatch()
    routes = [
        Route(path, endpoint=dispatch, methods=methods)
        for path, methods in AUTH_SERVER_ROUTES.items()
    ]
    routes.append(
        Route(
            PROTECTED_RESOURCE_METADATA_PATH,
            endpoint=_ProtectedResourceDispatch(),
            methods=["GET", "OPTIONS"],
        )
    )
    return routes
