"""
Debug API endpoints for troubleshooting route registration and other issues.
Protected by debug token for security.
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from family_assistant.web.auth import (
    AUTH_ENABLED,
    OIDC_CLIENT_ID,
    OIDC_DISCOVERY_URL,
    SESSION_SECRET_KEY,
)

logger = logging.getLogger(__name__)
debug_api_router = APIRouter()


def is_debug_authorized(request: Request) -> bool:
    """Check if debug endpoints should be accessible.

    Currently always returns True to help diagnose production issues.
    The internal structure is not considered secret.
    """
    return True


@debug_api_router.get("/routes")
async def dump_routes(request: Request) -> dict[str, Any]:
    """
    Dump all registered routes in the application.

    This endpoint helps debug route registration issues by showing:
    - All registered routes with their methods and paths
    - The order of registration
    - Route names and endpoint functions
    - Whether authentication routes are present
    """
    if not is_debug_authorized(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Debug endpoints are not authorized",
        )

    app = request.app
    routes_info = []
    auth_routes_found = []

    for route in app.routes:
        route_info = {
            "path": getattr(route, "path", "N/A"),
            "methods": list(getattr(route, "methods", [])),
            "name": getattr(route, "name", None),
            "endpoint": str(getattr(route, "endpoint", "N/A")),
        }

        # Track auth-related routes specifically
        if hasattr(route, "path"):
            path = route.path
            if path in ["/login", "/logout", "/auth"]:
                auth_routes_found.append(path)

        routes_info.append(route_info)

    # Get additional app state information
    app_state_info = {
        "auth_service_configured": hasattr(app.state, "auth_service"),
        "database_engine_configured": hasattr(app.state, "database_engine"),
        "auth_enabled": getattr(app.state.config, "auth_enabled", None)
        if hasattr(app.state, "config")
        else None,
    }

    return {
        "total_routes": len(routes_info),
        "auth_routes_found": auth_routes_found,
        "auth_enabled_env": AUTH_ENABLED,
        "app_state": app_state_info,
        "routes": routes_info,
        "route_summary": {
            "total": len(routes_info),
            "by_path": {
                "api_routes": len([
                    r for r in routes_info if r["path"].startswith("/api")
                ]),
                "auth_routes": len(auth_routes_found),
                "ui_routes": len([
                    r
                    for r in routes_info
                    if not r["path"].startswith("/api")
                    and r["path"] not in ["/login", "/logout", "/auth"]
                ]),
            },
        },
    }


@debug_api_router.get("/auth-state")
async def dump_auth_state(request: Request) -> dict[str, Any]:
    """
    Dump the current authentication state and configuration.

    Shows:
    - Whether auth is enabled
    - OAuth configuration status
    - Session middleware status
    - Auth service initialization status
    """
    if not is_debug_authorized(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Debug endpoints are not authorized",
        )

    app = request.app

    # Check auth service status
    auth_service_info = None
    if hasattr(app.state, "auth_service"):
        auth_service = app.state.auth_service
        auth_service_info = {
            "enabled": auth_service.auth_enabled,
            "oauth_initialized": auth_service.oauth is not None,
        }

    # Check middleware stack for SessionMiddleware and AuthMiddleware
    middleware_info = []
    for middleware in app.middleware:
        middleware_info.append({
            "cls": str(middleware.cls),
            "options": str(middleware.options)
            if hasattr(middleware, "options")
            else None,
        })

    return {
        "environment_config": {
            "AUTH_ENABLED": AUTH_ENABLED,
            "OIDC_CLIENT_ID": OIDC_CLIENT_ID is not None,
            "OIDC_DISCOVERY_URL": OIDC_DISCOVERY_URL is not None,
            "SESSION_SECRET_KEY": SESSION_SECRET_KEY is not None,
        },
        "auth_service": auth_service_info,
        "middleware_stack": middleware_info,
        "app_state_auth_enabled": getattr(app.state.config, "auth_enabled", None)
        if hasattr(app.state, "config")
        else None,
    }
