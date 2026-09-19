"""OAuth 2.1 authorization server for MCP clients (docs/design/mcp-adapter.md).

The SDK supplies the protocol endpoints; ``provider`` supplies their state,
``routes`` gates them on ``mcp_adapter.enabled`` and binds them to the
configured ``server_url``, and ``consent`` is the page a user approves a
connection on.
"""

from fastapi import FastAPI

from family_assistant.web.mcp_adapter.oauth.consent import (
    CONSENT_ROUTE_NAME,
    consent_router,
)
from family_assistant.web.mcp_adapter.oauth.routes import oauth_routes


def install_oauth_routes(app: FastAPI) -> None:
    """Add the OAuth endpoints and consent page to ``app``.

    Idempotent: an app that already carries the consent route (its own, or
    copied from another app's router) is left alone.
    """
    if any(getattr(route, "name", None) == CONSENT_ROUTE_NAME for route in app.routes):
        return
    app.router.routes.extend(oauth_routes())
    app.include_router(consent_router, tags=["MCP OAuth"])
