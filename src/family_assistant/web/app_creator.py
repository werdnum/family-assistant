import json
import logging
import os
import pathlib

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware

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
from family_assistant.web.routers.documentation import documentation_router
from family_assistant.web.routers.documents_ui import (  # New import for document upload UI
    router as documents_ui_router,
)
from family_assistant.web.routers.health import health_router
from family_assistant.web.routers.history import history_router
from family_assistant.web.routers.notes import notes_router
from family_assistant.web.routers.tasks_ui import tasks_ui_router
from family_assistant.web.routers.tools_ui import tools_ui_router
from family_assistant.web.routers.ui_token_management import (  # New import
    router as ui_token_management_router,
)
from family_assistant.web.routers.vector_search import vector_search_router
from family_assistant.web.routers.webhooks import webhooks_router

logger = logging.getLogger(__name__)


# Load server URL from environment variable (used in templates)
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
    docs_user_dir = _project_root / "docs" / "user"

    if not templates_dir.is_dir():
        logger.warning(
            f"Templates directory not found at expected location: {templates_dir}"
        )
    if not static_dir.is_dir():
        logger.warning(f"Static directory not found at expected location: {static_dir}")

    templates = Jinja2Templates(directory=templates_dir)
    templates.env.filters["tojson"] = json.dumps

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

app.include_router(notes_router, tags=["Notes UI"])
app.include_router(documentation_router, tags=["Documentation UI"])
app.include_router(webhooks_router, tags=["Webhooks"])
app.include_router(history_router, tags=["History UI"])
app.include_router(tools_ui_router, tags=["Tools UI"])
app.include_router(tasks_ui_router, tags=["Tasks UI"])
app.include_router(vector_search_router, tags=["Vector Search UI"])
app.include_router(documents_ui_router, prefix="/documents", tags=["Documents UI"]) # New router
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
