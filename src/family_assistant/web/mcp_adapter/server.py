"""The mounted MCP endpoint: transport, enablement gate and lifecycle."""

import logging
from contextlib import AbstractAsyncContextManager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from family_assistant.web.auth import MCP_ENDPOINT_PATH
from family_assistant.web.mcp_adapter.config import adapter_config
from family_assistant.web.mcp_adapter.oauth import install_oauth_routes
from family_assistant.web.mcp_adapter.tools import register_tools

logger = logging.getLogger(__name__)

SERVER_NAME = "Family Assistant"
SERVER_INSTRUCTIONS = (
    "Family Assistant is a household assistant with access to the family's notes, "
    "calendar, tasks, documents and smart home. Ask it questions in natural language "
    "with ask_family_assistant; pass back the conversation_id it returns to continue "
    "the same conversation."
)


class _EnabledGate:
    """404 the endpoint while ``mcp_adapter.enabled`` is off."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not adapter_config(scope["app"]).enabled:
            response = JSONResponse(
                status_code=404, content={"detail": "MCP adapter is not enabled."}
            )
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


class MCPAdapter:
    """The MCP server and the ASGI handler that serves it.

    The handler is the SDK's raw ASGI app rather than the Starlette app it can
    wrap it in: a Starlette sub-app rebinds ``scope["app"]`` to itself, and the
    tool needs the outer application's ``state`` (processing services, engine,
    config) exactly as a router does.
    """

    def __init__(self) -> None:
        self.mcp = FastMCP(
            SERVER_NAME,
            instructions=SERVER_INSTRUCTIONS,
            stateless_http=True,
            json_response=True,
        )
        register_tools(self.mcp)
        # Builds the session manager; the Starlette app it returns is unused.
        self.mcp.streamable_http_app()
        self.asgi_app: ASGIApp = _EnabledGate(
            StreamableHTTPASGIApp(self.mcp.session_manager)
        )

    def run(self) -> AbstractAsyncContextManager[None]:
        """The session manager's lifetime; enter it for the app's lifespan."""
        return self.mcp.session_manager.run()


def install_mcp_adapter(app: FastAPI) -> MCPAdapter:
    """Mount the MCP endpoint and OAuth routes on ``app``.

    Idempotent per app: a second call returns the adapter already installed.
    """
    existing = getattr(app.state, "mcp_adapter", None)
    if isinstance(existing, MCPAdapter):
        return existing
    adapter = MCPAdapter()
    # An exact route rather than a mount: a Starlette mount at ``/api/mcp`` only
    # matches paths beneath it, and the bare endpoint is what MCP clients call.
    app.router.routes.append(
        Route(
            MCP_ENDPOINT_PATH,
            adapter.asgi_app,
            methods=["GET", "POST", "DELETE"],
            name="mcp_adapter",
            include_in_schema=False,
        )
    )
    install_oauth_routes(app)
    app.state.mcp_adapter = adapter
    logger.debug("MCP adapter mounted at %s", MCP_ENDPOINT_PATH)
    return adapter
