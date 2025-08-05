import json
import logging
import os
import pathlib
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import _TemplateResponse

# Import new auth and utils modules
from family_assistant.web.auth import (
    AUTH_ENABLED,
    PUBLIC_PATHS,
    SESSION_SECRET_KEY,
    AuthMiddleware,
    auth_router,
)
from family_assistant.web.routers.api import api_router
from family_assistant.web.routers.api_token_management import (
    router as api_token_management_router,
)
from family_assistant.web.routers.context_viewer import context_viewer_router
from family_assistant.web.routers.documentation import documentation_router
from family_assistant.web.routers.documents_ui import (  # New import for document upload UI
    router as documents_ui_router,
)
from family_assistant.web.routers.errors import router as errors_router
from family_assistant.web.routers.health import health_router
from family_assistant.web.routers.ui_token_management import (  # New import
    router as ui_token_management_router,
)
from family_assistant.web.routers.vector_search import vector_search_router
from family_assistant.web.routers.vite_pages import vite_pages_router
from family_assistant.web.routers.webhooks import webhooks_router
from family_assistant.web.template_utils import get_static_asset

logger = logging.getLogger(__name__)


# Load server URL from environment variable (used in templates)
# Default to production URL - dev mode will be determined at runtime
SERVER_URL = os.getenv("SERVER_URL", "http://localhost:8000")


# --- Determine base path for templates and static files ---
try:
    # __file__ is src/family_assistant/web/app_creator.py
    # Project root is 4 levels up from app_creator.py
    _project_root = pathlib.Path(__file__).parent.parent.parent.parent.resolve()
    # Package root (src/family_assistant/) is 2 levels up from app_creator.py
    package_root_dir = pathlib.Path(__file__).parent.parent.resolve()

    templates_dir = package_root_dir / "templates"
    static_dir = package_root_dir / "static"

    # Allow docs directory to be configured via environment variable for Docker deployments
    docs_user_dir_env = os.getenv("DOCS_USER_DIR")
    if docs_user_dir_env:
        docs_user_dir = pathlib.Path(docs_user_dir_env).resolve()
        logger.info(f"Using DOCS_USER_DIR from environment: {docs_user_dir}")
    else:
        docs_user_dir = _project_root / "docs" / "user"
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
    logger.info("SessionMiddleware added (SESSION_SECRET_KEY is set).")
else:
    logger.warning(
        "SessionMiddleware NOT added (SESSION_SECRET_KEY is not set). Accessing request.session will fail, which might break OIDC if it were enabled."
    )

if AUTH_ENABLED:
    middleware.append(
        Middleware(AuthMiddleware, public_paths=PUBLIC_PATHS, auth_enabled=AUTH_ENABLED)
    )
    logger.info("AuthMiddleware added to the application middleware stack.")
else:
    logger.info("AuthMiddleware NOT added as AUTH_ENABLED is false.")


# --- FastAPI App Initialization ---
app = FastAPI(
    title="Family Assistant Web Interface",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    middleware=middleware,
)

# --- Store shared objects on app.state ---
app.state.templates = templates
app.state.server_url = SERVER_URL
app.state.docs_user_dir = docs_user_dir


# --- Configure template helpers ---
def get_dev_mode_from_request(request: Request) -> bool:
    """Get dev_mode from app config if available, otherwise from environment."""
    if hasattr(request.app.state, "config") and "dev_mode" in request.app.state.config:
        return request.app.state.config.get("dev_mode", False)
    # Fallback to environment variable
    return os.getenv("DEV_MODE", "false").lower() == "true"


def create_template_context(request: Request, **kwargs: Any) -> dict[str, Any]:
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

    def TemplateResponse(self, *args: Any, **kwargs: Any) -> _TemplateResponse:
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
            else:  # new style
                if len(args) > 2:
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

# Initialize tool_definitions for development mode
# This will be populated by Assistant.setup_dependencies() in production
# For development, we load them directly here
if not hasattr(app.state, "tool_definitions"):
    try:
        from family_assistant.tools import TOOLS_DEFINITION

        app.state.tool_definitions = TOOLS_DEFINITION
        logger.info(
            f"Loaded {len(TOOLS_DEFINITION)} tool definitions for development mode"
        )
    except ImportError as e:
        app.state.tool_definitions = []
        logger.warning(f"Could not import tool definitions for development: {e}")


def configure_app_debug(debug: bool = True) -> None:
    """Configure the FastAPI app debug mode. Useful for tests to get detailed error messages."""
    app.debug = debug
    logger.info(f"FastAPI debug mode set to: {debug}")


# --- Mount Static Files ---
if "static_dir" in locals() and static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    logger.info(f"Mounted static files from: {static_dir}")
else:
    logger.error(
        f"Static directory '{static_dir if 'static_dir' in locals() else 'Not Defined'}' not found or not a directory. Static files will not be served."
    )

# --- Include Routers ---
if AUTH_ENABLED:
    app.include_router(auth_router, tags=["Authentication"])
    logger.info("Authentication routes included.")

app.include_router(vite_pages_router, tags=["Vite Pages"])
app.include_router(documentation_router, tags=["Documentation UI"])
app.include_router(webhooks_router, tags=["Webhooks"])
app.include_router(vector_search_router, tags=["Vector Search UI"])
app.include_router(context_viewer_router, tags=["Context Viewer UI"])
app.include_router(
    documents_ui_router, prefix="/documents", tags=["Documents UI"]
)  # New router
app.include_router(errors_router, tags=["Error Logs UI"])
app.include_router(health_router, tags=["Health Check"])

# General API endpoints (like /api/tools/execute, /api/documents/upload)
app.include_router(api_router, prefix="/api", tags=["General API"])

# API Token Management endpoints (like /api/me/tokens)
# This is nested under /api as well, so the full path would be /api/me/tokens
app.include_router(
    api_token_management_router,
    prefix="/api/me/tokens",  # Suggesting a "me" scope for user-specific tokens
    tags=["API Token Management"],
)

# UI for API Token Management
app.include_router(
    ui_token_management_router,
    prefix="/settings/tokens",  # UI page for managing tokens
    tags=["Settings UI"],
)


# --- Serve Vite-built HTML files in production ---
# Always add this handler, but it will check dev mode at runtime
@app.get("/{path:path}", include_in_schema=False, response_model=None)
async def serve_vite_html(request: Request, path: str) -> FileResponse:
    """
    Serve Vite-built HTML files from the dist directory in production mode.
    This handler runs after all other routes, acting as a fallback.
    """
    # Check if we're in dev mode at runtime
    config = getattr(request.app.state, "config", {})
    dev_mode = config.get("dev_mode", os.getenv("DEV_MODE", "false").lower() == "true")

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


# Export the helper functions and app
__all__ = [
    "app",
    "create_template_context",
    "get_dev_mode_from_request",
    "configure_app_debug",
]
