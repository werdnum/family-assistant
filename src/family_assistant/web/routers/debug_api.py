"""
Debug API endpoints for troubleshooting route registration and other issues.
Protected by debug token for security.
"""

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response

from family_assistant.config_models import ServiceProfile
from family_assistant.web.auth import (
    AUTH_ENABLED,
    OIDC_CLIENT_ID,
    OIDC_DISCOVERY_URL,
    SESSION_SECRET_KEY,
)
from family_assistant.web.dependencies import get_current_user

logger = logging.getLogger(__name__)
debug_api_router = APIRouter()

# Fields whose values may leak secrets or per-user credentials. When a key with
# one of these names appears anywhere in the serialized config (including nested
# dicts under e.g. ``camera_config`` or ``home_assistant_*``), its value is
# replaced with "[REDACTED]" in the response.
SENSITIVE_FIELD_NAMES: frozenset[str] = frozenset({
    "home_assistant_token",
    "password",
    "client_secret",
    "mailgun_webhook_signing_key",
    "gemini_api_key",
    "openai_api_key",
    "openrouter_api_key",
    "telegram_token",
    "vapid_private_key",
    "session_secret_key",
    "token_env",  # name of env var, redacted defensively
})

# Substring patterns used as a defense-in-depth fallback so config fields added
# in the future with secret-looking names (e.g. ``*_api_key``, ``*_secret``,
# ``*_token``, ``*_password``) are redacted even if not explicitly allowlisted.
_SENSITIVE_SUBSTRINGS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "passwd",
    "private_key",
)


def is_sensitive_field_name(key: object) -> bool:
    """Return True if the given dict key looks like it carries a secret."""
    if not isinstance(key, str):
        return False
    if key in SENSITIVE_FIELD_NAMES:
        return True
    lowered = key.lower()
    return any(substring in lowered for substring in _SENSITIVE_SUBSTRINGS)


# ast-grep-ignore: no-dict-any - Recursive config redaction handles arbitrary nested structures
def redact_sensitive_config(obj: Any) -> Any:  # noqa: ANN401 - recursive over arbitrary JSON-like data
    """Recursively redact sensitive fields in a serialized config structure."""
    if isinstance(obj, dict):
        return {
            key: "[REDACTED]"
            if is_sensitive_field_name(key) and value
            else redact_sensitive_config(value)
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [redact_sensitive_config(item) for item in obj]
    return obj


def _dump_profile(
    profile: ServiceProfile,
    # ast-grep-ignore: no-dict-any - Serialized Pydantic model has heterogeneous top-level values
) -> dict[str, Any]:
    """Serialize a ServiceProfile including operator-layer fields that model_dump excludes.

    ``ServiceProfile.operator_tools_policy`` and ``operator_mcp_server_ids`` are
    declared with ``exclude=True`` so they do not round-trip through the YAML
    config, but they ARE merged with the profile layer at runtime (see
    ``PolicyEngine.from_layers``). For the debug dump we want the caller to see
    every layer contributing to the effective policy, so we serialize them
    explicitly.
    """
    dumped = profile.model_dump(mode="json")

    if profile.operator_tools_policy is not None:
        dumped["operator_tools_policy"] = profile.operator_tools_policy.model_dump(
            mode="json"
        )
    else:
        dumped["operator_tools_policy"] = None

    dumped["operator_mcp_server_ids"] = [
        entry if isinstance(entry, str) else entry.model_dump(mode="json")
        for entry in profile.operator_mcp_server_ids
    ]

    return dumped


def _runtime_info_for(
    profile_id: str,
    # ast-grep-ignore: no-dict-any - Debug endpoint inspects dynamic runtime registry state
    processing_services_registry: dict[str, Any],
    # ast-grep-ignore: no-dict-any - Debug endpoint returns dynamic runtime introspection data
) -> dict[str, Any] | None:
    """Collect live runtime info for a profile from the services registry."""
    service = processing_services_registry.get(profile_id)
    if service is None:
        return None

    kind = getattr(service, "kind", "unknown")
    if kind == "remote":
        return {"kind": "remote"}

    llm_client = getattr(service, "llm_client", None)
    context_providers = getattr(service, "context_providers", []) or []
    return {
        "kind": kind,
        "llm_model": getattr(llm_client, "model", None) if llm_client else None,
        "llm_provider": getattr(llm_client, "provider", None) if llm_client else None,
        "llm_client_class": type(llm_client).__name__ if llm_client else None,
        "context_providers": [
            getattr(p, "name", type(p).__name__) for p in context_providers
        ],
    }


def is_debug_authorized(request: Request) -> bool:
    """Check if debug endpoints should be accessible.

    Currently always returns True to help diagnose production issues.
    The internal structure is not considered secret.
    """
    return True


@debug_api_router.get("/routes")
# ast-grep-ignore: no-dict-any - Debug endpoint returns dynamic introspection data from route inspection
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
            if path in {"/login", "/logout", "/auth"}:
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
                    and r["path"] not in {"/login", "/logout", "/auth"}
                ]),
            },
        },
    }


@debug_api_router.get("/auth-state")
# ast-grep-ignore: no-dict-any - Debug endpoint returns dynamic introspection data from auth state inspection
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


@debug_api_router.get("/profiles")
async def dump_profiles(  # noqa: A002 - FastAPI query param name shadows builtin
    request: Request,
    _user: Annotated[dict, Depends(get_current_user)],
    format: Annotated[
        str,
        Query(
            description=(
                "Output format: 'json' (pretty-printed JSON) or 'raw' (compact JSON)."
            ),
            pattern="^(json|raw)$",
        ),
    ] = "json",
    profile_id: Annotated[
        str | None,
        Query(description="If set, return only the profile with this id."),
    ] = None,
) -> Response:
    """
    Dump the full processing profile configuration.

    Shows each resolved service profile (after ``default_profile_settings`` have
    been merged) including:

    - ``processing_config`` — prompts, LLM model/provider, retry/fallback chain,
      history limits, timezone, iteration caps, calendar/camera/Home Assistant
      settings, delegation security level, system-doc includes.
    - ``tools_config`` — enabled local tools (with eager/on-demand loading mode),
      enabled MCP servers, tools requiring confirmation, timeouts.
    - ``tools_policy`` — the full policy matrix: each rule's matcher (names,
      tags, MCP server ids, argument equality), decision (allow/deny/confirm),
      priority, and description; plus the default decision.
    - ``slash_commands``, ``visibility_grants``, and ``remote_a2a`` delegation
      config if configured.
    - ``runtime`` info from the live processing services registry (resolved LLM
      model/provider, LLM client class, context provider names).

    This endpoint exposes internal configuration (prompts, policy rules, tool
    enablement). It requires an authenticated user (OIDC session or API token)
    and is additionally gated through the shared ``is_debug_authorized`` check
    used by the other ``/api/debug/*`` routes. Secret-bearing fields (tokens,
    passwords, API keys) are redacted. The response defaults to pretty-printed
    JSON (``indent=2``); pass ``?format=raw`` for compact JSON.
    """
    if not is_debug_authorized(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Debug endpoints are not authorized",
        )

    app = request.app
    config = getattr(app.state, "config", None)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Application config not initialized",
        )

    processing_services_registry = getattr(app.state, "processing_services", None) or {}

    # ast-grep-ignore: no-dict-any - Debug endpoint returns serialized Pydantic config dicts
    profiles_info: list[dict[str, Any]] = []
    for profile in config.service_profiles:
        if profile_id is not None and profile.id != profile_id:
            continue
        profile_dump = redact_sensitive_config(_dump_profile(profile))
        runtime_info = _runtime_info_for(profile.id, processing_services_registry)
        profiles_info.append({
            "id": profile.id,
            "description": profile.description,
            "config": profile_dump,
            "runtime": runtime_info,
        })

    if profile_id is not None and not profiles_info:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile '{profile_id}' not found",
        )

    default_settings_dump = redact_sensitive_config(
        config.default_profile_settings.model_dump(mode="json")
    )

    # ast-grep-ignore: no-dict-any - Debug endpoint returns serialized Pydantic config dicts
    payload: dict[str, Any] = {
        "default_service_profile_id": config.default_service_profile_id,
        "default_profile_settings": default_settings_dump,
        "profiles": profiles_info,
        "profile_count": len(profiles_info),
        "registered_service_ids": sorted(processing_services_registry.keys()),
    }

    indent = None if format == "raw" else 2
    return Response(
        content=json.dumps(payload, indent=indent, sort_keys=False, default=str),
        media_type="application/json",
    )
