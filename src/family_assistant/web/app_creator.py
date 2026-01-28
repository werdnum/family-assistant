import json
import logging
import os
import pathlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import _TemplateResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from family_assistant.constants import PACKAGE_ROOT, PROJECT_ROOT

# Import new auth and utils modules
from family_assistant.web.auth import (
    AUTH_ENABLED,
    PUBLIC_PATHS,
    SESSION_SECRET_KEY,
    AuthMiddleware,
    AuthService,
    create_auth_router,
)
from family_assistant.web.routers.api import api_router
from family_assistant.web.routers.api_documentation import (
    router as api_documentation_router,
)
from family_assistant.web.routers.api_token_management import (
    router as api_token_management_router,
)
from family_assistant.web.routers.asterisk_live_api import asterisk_live_router
from family_assistant.web.routers.client_config import router as client_config_router
from family_assistant.web.routers.context_viewer import context_viewer_router
from family_assistant.web.routers.gemini_live_api import gemini_live_router

# documents_ui, vector_search, and errors routers removed - replaced with React
from family_assistant.web.routers.health import health_router
from family_assistant.web.routers.push import router as push_router
from family_assistant.web.routers.vite_pages import vite_pages_router
from family_assistant.web.routers.webhooks import webhooks_router
from family_assistant.web.template_utils import get_static_asset

logger = logging.getLogger(__name__)


# Load server URL from environment variable (used in templates)
# Default to production URL - dev mode will be determined at runtime
SERVER_URL = os.getenv("SERVER_URL", "http://localhost:8000")


# --- Determine base path for templates and static files ---
try:
    templates_dir = PACKAGE_ROOT / "templates"
    static_dir = PACKAGE_ROOT / "static"

    # Allow docs directory to be configured via environment variable for Docker deployments
    docs_user_dir_env = os.getenv("DOCS_USER_DIR")
    if docs_user_dir_env:
        docs_user_dir = pathlib.Path(docs_user_dir_env).resolve()
        logger.info(f"Using DOCS_USER_DIR from environment: {docs_user_dir}")
    else:
        docs_user_dir = PROJECT_ROOT / "docs" / "user"
        # In Docker, if the calculated path doesn't exist, try /app/docs/user
        if not docs_user_dir.exists() and pathlib.Path("/app/docs/user").exists():
            docs_user_dir = pathlib.Path("/app/docs/user")
            logger.info(f"Using Docker default docs directory: {docs_user_dir}")

    if not templates_dir.is_dir():
        logger.warning(
            f"Templates directory not found at expected location: {templates_dir}"
        )
    if not static_dir.is_dir():
        logger.warning(f"Static directory not found at expected location: {static_dir}")
    if not docs_user_dir.is_dir():
        logger.warning(
            f"User docs directory not found at expected location: {docs_user_dir}"
        )

    templates = Jinja2Templates(directory=templates_dir)
    templates.env.filters["tojson"] = json.dumps
    templates.env.globals["AUTH_ENABLED"] = AUTH_ENABLED

except NameError:
    logger.error(
        "Could not determine package path using __file__. Static/template files might not load."
    )
    # Fallback paths, assuming execution from project root if __file__ is not defined.
    # This is less likely to be hit with proper packaging.
    templates = Jinja2Templates(directory="src/family_assistant/templates")
    static_dir = pathlib.Path("src/family_assistant/static")
    docs_user_dir = pathlib.Path("docs") / "user"
    logger.warning(f"Using fallback user docs directory: {docs_user_dir}")


middleware = []

if SESSION_SECRET_KEY:
    middleware.append(Middleware(SessionMiddleware, secret_key=SESSION_SECRET_KEY))
else:
    logger.warning(
        "SessionMiddleware NOT added (SESSION_SECRET_KEY is not set). Accessing request.session will fail, which might break OIDC if it were enabled."
    )


# Create a wrapper for AuthMiddleware that will get AuthService from app.state
class AuthMiddlewareWrapper:
    """Wrapper that gets AuthService from app.state at runtime."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.auth_service: AuthService | None = None
        self.auth_middleware: AuthMiddleware | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Get AuthService from app.state if not already cached
        if self.auth_middleware is None and "app" in scope:
            app = scope["app"]
            if hasattr(app.state, "auth_service"):
                self.auth_service = app.state.auth_service
                if self.auth_service is not None:
                    self.auth_middleware = AuthMiddleware(
                        self.app, self.auth_service, PUBLIC_PATHS
                    )

        # Use AuthMiddleware if available and auth is enabled
        if self.auth_middleware and AUTH_ENABLED:
            await self.auth_middleware(scope, receive, send)
        else:
            await self.app(scope, receive, send)


if AUTH_ENABLED:
    middleware.append(Middleware(AuthMiddlewareWrapper))
else:
    logger.info("AuthMiddleware NOT added as AUTH_ENABLED is false.")


# --- Lifespan context manager for startup/shutdown ---
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage application lifecycle events."""
    # Startup
    logger.info("Application starting up...")

    # Initialize AuthService if database engine is available
    # Note: database_engine will be set by Assistant during setup
    if hasattr(app.state, "database_engine"):
        app.state.auth_service = AuthService(app.state.database_engine)
        logger.info("AuthService initialized with database engine")

        # Initialize WebChatInterface for web UI message delivery
        from family_assistant.web.web_chat_interface import (  # noqa: PLC0415
            WebChatInterface,
        )

        # Retrieve push service from app.state (injected by Assistant)
        push_notification_service = getattr(
            app.state, "push_notification_service", None
        )

        app.state.web_chat_interface = WebChatInterface(
            app.state.database_engine,
            push_notification_service=push_notification_service,
        )
        # Register web chat interface in the registry
        if not hasattr(app.state, "chat_interfaces"):
            app.state.chat_interfaces = {}
        app.state.chat_interfaces["web"] = app.state.web_chat_interface
        logger.info("WebChatInterface initialized with database engine")
    else:
        # For development or when database is not yet initialized
        app.state.auth_service = AuthService()
        logger.warning(
            "AuthService initialized without database engine - API token auth will not work"
        )
        app.state.web_chat_interface = None

    yield

    # Shutdown
    logger.info("Application shutting down...")


# --- FastAPI App Factory ---
def create_app() -> FastAPI:
    """Create a new FastAPI application instance.

    This factory function creates a fresh FastAPI instance with all middleware,
    routes, and configuration. Each instance has isolated state, preventing
    concurrent modifications when multiple apps are created (e.g., in tests).

    Returns:
        FastAPI: A configured FastAPI application instance
    """
    new_app = FastAPI(
        title="Family Assistant Web Interface",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        middleware=middleware,
        lifespan=lifespan,
    )

    # --- Store shared objects on app.state ---
    new_app.state.templates = templates
    new_app.state.server_url = SERVER_URL
    new_app.state.docs_user_dir = docs_user_dir

    # Initialize tool_definitions for development mode
    # This will be populated by Assistant.setup_dependencies() in production
    # For development, we load them directly here
    if not hasattr(new_app.state, "tool_definitions"):
        try:
            from family_assistant.tools import TOOLS_DEFINITION  # noqa: PLC0415

            new_app.state.tool_definitions = TOOLS_DEFINITION
        except ImportError as e:
            new_app.state.tool_definitions = []
            logger.warning(f"Could not import tool definitions for development: {e}")

    # --- Mount Static Files ---
    if static_dir.is_dir():
        new_app.mount("/static", StaticFiles(directory=static_dir), name="static")
        logger.debug(f"Mounted static files from: {static_dir}")
    else:
        logger.error(
            f"Static directory '{static_dir}' not found or not a directory. Static files will not be served."
        )

    # --- Include Routers ---
    # Note: Auth router will be added after AuthService is initialized

    logger.debug("Including vite_pages_router...")
    new_app.include_router(vite_pages_router, tags=["Vite Pages"])

    # Log registered UI routes for debugging CI issues
    ui_routes = []
    for route in vite_pages_router.routes:
        if hasattr(route, "path") and hasattr(route, "methods"):
            methods = getattr(route, "methods", set())
            path = getattr(route, "path", "unknown")
            method = list(methods)[0] if methods else "GET"
            ui_routes.append(f"{method} {path}")
    logger.debug(f"Registered UI routes from vite_pages_router: {ui_routes}")

    # Check if any vite routes might conflict with auth routes
    auth_paths = {"/login", "/logout", "/auth"}
    # Extract paths from route strings and check for conflicts efficiently
    ui_route_paths = set()
    conflicting_paths = []
    for route in ui_routes:
        split_route = route.split()
        if len(split_route) >= 2:
            ui_route_paths.add(split_route[1])
            if any(
                split_route[1] == auth_path
                or split_route[1].startswith(auth_path + "/")
                for auth_path in auth_paths
            ):
                conflicting_paths.append(route)
    if conflicting_paths:
        logger.warning(
            f"Potential route conflicts with auth paths: {conflicting_paths}"
        )

    new_app.include_router(webhooks_router, tags=["Webhooks"])
    new_app.include_router(context_viewer_router, tags=["Context Viewer UI"])
    new_app.include_router(health_router, tags=["Health Check"])

    # Client configuration and push notification endpoints
    new_app.include_router(client_config_router, tags=["Client Configuration"])
    new_app.include_router(push_router, tags=["Push Notifications"])

    # General API endpoints (like /api/tools/execute, /api/documents/upload)
    new_app.include_router(api_router, prefix="/api", tags=["General API"])

    # Gemini Live API endpoints for voice mode
    new_app.include_router(gemini_live_router, prefix="/api", tags=["Gemini Live API"])

    # Asterisk Live API endpoints
    new_app.include_router(
        asterisk_live_router, prefix="/api", tags=["Asterisk Live API"]
    )

    # API Token Management endpoints (like /api/me/tokens)
    # This is nested under /api as well, so the full path would be /api/me/tokens
    new_app.include_router(
        api_documentation_router,
        prefix="/api/documentation",
        tags=["Documentation"],
    )
    new_app.include_router(
        api_token_management_router,
        prefix="/api/me/tokens",  # Suggesting a "me" scope for user-specific tokens
        tags=["API Token Management"],
    )

    return new_app


# --- FastAPI App Initialization (Module-Level Singleton) ---
# This module-level app is kept for backward compatibility with:
# - CLI usage: `uvicorn family_assistant.web.app_creator:app`
# - Direct imports from existing code
# New code should use create_app() to get isolated instances
app = create_app()


# --- Configure template helpers ---
def get_dev_mode_from_request(request: Request) -> bool:
    """Get dev_mode from app config if available, otherwise from environment."""
    if hasattr(request.app.state, "config") and hasattr(
        request.app.state.config, "dev_mode"
    ):
        return request.app.state.config.dev_mode
    # Fallback to environment variable
    return os.getenv("DEV_MODE", "false").lower() == "true"


def create_template_context(
    request: Request,
    **kwargs: str | int | float | bool | None,
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
) -> dict[str, Any]:
    """Create a template context with common variables including dev_mode."""
    dev_mode = get_dev_mode_from_request(request)

    # Create context-aware functions
    def context_get_static_asset(filename: str, entry_name: str = "main") -> str:
        return get_static_asset(filename, entry_name, dev_mode)

    context = {
        "request": request,
        "DEV_MODE": dev_mode,
        "get_static_asset": context_get_static_asset,
        **kwargs,
    }
    return context


# Set template globals
templates.env.globals["AUTH_ENABLED"] = AUTH_ENABLED


# Create a custom template response handler that uses inheritance
class DevModeTemplates(Jinja2Templates):
    """Custom Jinja2Templates that injects dev_mode into context."""

    def TemplateResponse(
        self,
        *args: Any,  # noqa: ANN401 # Complex template args with multiple possible types
        **kwargs: Any,  # noqa: ANN401 # Complex template kwargs with multiple possible types
    ) -> _TemplateResponse:
        """Override to inject dev_mode and context-aware functions."""
        # First, let the parent class parse the arguments to get the request and context
        # We need to extract the request to determine dev_mode

        # Parse arguments similar to parent class
        if args:
            if isinstance(args[0], str):  # old style: name first
                context = args[1] if len(args) > 1 else kwargs.get("context", {})
                request = context.get("request") if context else None
            else:  # new style: request first
                request = args[0]
                context = args[2] if len(args) > 2 else kwargs.get("context", {})
        else:  # all kwargs
            context = kwargs.get("context", {})
            request = kwargs.get("request", context.get("request"))

        # Ensure context exists
        if context is None:
            context = {}

        # Update args/kwargs with modified context
        if args:
            if isinstance(args[0], str):  # old style
                if len(args) > 1:
                    args = (args[0], context) + args[2:]
                else:
                    kwargs["context"] = context
            elif len(args) > 2:  # new style with explicit context arg
                args = args[:2] + (context,) + args[3:]
            else:
                kwargs["context"] = context
        else:
            kwargs["context"] = context

        # Inject our custom context
        if isinstance(request, Request):
            dev_mode = get_dev_mode_from_request(request)
            # Add dev_mode function and context-aware get_static_asset
            if "DEV_MODE" not in context:
                # Make it a callable that returns the dev_mode value
                context["DEV_MODE"] = lambda: dev_mode
            if "get_static_asset" not in context:

                def context_get_static_asset(
                    filename: str, entry_name: str = "main"
                ) -> str:
                    return get_static_asset(filename, entry_name, dev_mode)

                context["get_static_asset"] = context_get_static_asset

        # Call parent with potentially modified args
        return super().TemplateResponse(*args, **kwargs)


# Replace the templates instance with our custom class
templates.__class__ = DevModeTemplates


def configure_app_auth(
    app: FastAPI, database_engine: AsyncEngine | None = None
) -> None:
    """Configure authentication for the app with proper dependency injection.

    This should be called after the app is created and database engine is available.
    Typically called by Assistant.setup_dependencies().
    """
    # Initialize AuthService
    auth_service = AuthService(database_engine)
    app.state.auth_service = auth_service

    # Include auth router
    if AUTH_ENABLED:
        auth_router = create_auth_router(auth_service)

        # Verify routes were actually added
        route_count = len(auth_router.routes)
        if route_count == 0:
            logger.error(
                "Auth router has no routes despite AUTH_ENABLED=True. "
                "This should not happen - create_auth_router should have added fallback routes."
            )

        app.include_router(auth_router, tags=["Authentication"])

        # Log the actual routes for debugging
        route_paths = []
        for route in auth_router.routes:
            if hasattr(route, "path"):
                route_path = getattr(route, "path", "unknown")
                route_paths.append(route_path)
                logger.debug(f"  Auth route registered: {route_path}")

        logger.info(
            f"Authentication routes included with AuthService "
            f"({route_count} routes added: {', '.join(route_paths)})"
        )

        # Additional check: verify /login is actually accessible
        login_found = any(
            hasattr(r, "path") and getattr(r, "path", "") == "/login"
            for r in auth_router.routes
        )
        if not login_found:
            logger.error(
                "CRITICAL: /login route not found in auth router despite AUTH_ENABLED=True"
            )
    else:
        logger.info("Authentication not configured (AUTH_ENABLED=False)")

    # Store auth configuration in app state for dependencies
    app.state.auth_enabled = AUTH_ENABLED

    # IMPORTANT: Register catch-all route AFTER auth routes to prevent it from
    # intercepting /login, /logout, /auth paths
    @app.get("/{path:path}", include_in_schema=False, response_model=None)
    async def serve_vite_html(request: Request, path: str) -> FileResponse:
        """
        Serve Vite-built HTML files from the dist directory in production mode.
        This handler runs after all other routes, acting as a fallback.
        """
        # Check if we're in dev mode at runtime
        config = getattr(request.app.state, "config", None)
        if config and hasattr(config, "dev_mode"):
            dev_mode = config.dev_mode
        else:
            dev_mode = os.getenv("DEV_MODE", "false").lower() == "true"

        if dev_mode:
            # In dev mode, let the default 404 handler run (Vite will serve the files)
            raise HTTPException(status_code=404, detail="Not found")

        # Only handle paths that could be HTML pages (no extension or .html)
        if path and not path.endswith(".html") and "." in path:
            # Has an extension but not .html, let default 404 handle it
            raise HTTPException(status_code=404, detail="Not found")

        # Check if there's a corresponding HTML file in dist
        html_filename = (
            f"{path}.html"
            if path and not path.endswith(".html")
            else (path or "index.html")
        )
        if not html_filename.endswith(".html"):
            html_filename += ".html"

        html_path = static_dir / "dist" / html_filename

        if html_path.exists() and html_path.is_file():
            return FileResponse(html_path, media_type="text/html")

        # No matching HTML file, return 404
        raise HTTPException(status_code=404, detail="Not found")

    logger.info("Catch-all route registered for serving Vite HTML files")

    # Log ALL application routes for debugging
    logger.info("=== ALL APPLICATION ROUTES AFTER AUTH CONFIGURATION ===")
    all_routes = []
    for route in app.routes:
        if hasattr(route, "path"):
            route_path = getattr(route, "path", "unknown")
            methods = getattr(route, "methods", set())
            method_str = ",".join(methods) if methods else "ANY"
            all_routes.append(f"{method_str} {route_path}")
            logger.debug(f"  App route: {method_str} {route_path}")

    # Check if /login is in the final application routes
    login_in_app = any("/login" in r for r in all_routes)
    if AUTH_ENABLED and not login_in_app:
        logger.error("CRITICAL: /login route is NOT in final application routes!")
    logger.info(f"Total application routes: {len(all_routes)}")


# Export the helper functions and app
__all__ = [
    "app",
    "create_app",
    "create_template_context",
    "get_dev_mode_from_request",
    "configure_app_auth",
]
