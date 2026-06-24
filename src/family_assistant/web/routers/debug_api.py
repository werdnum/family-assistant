"""
Debug API endpoints for troubleshooting route registration and other issues.
Protected by debug token for security.
"""

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response

from family_assistant.config_inspection import (
    SENSITIVE_FIELD_NAMES,
    dump_profile_like,
    is_sensitive_field_name,
    redact_sensitive_config,
)
from family_assistant.tool_inventory import ToolInventory, build_tool_inventory
from family_assistant.web.auth import (
    AUTH_ENABLED,
    OIDC_CLIENT_ID,
    OIDC_DISCOVERY_URL,
    SESSION_SECRET_KEY,
)
from family_assistant.web.dependencies import get_current_user, get_diagnostics_reader

logger = logging.getLogger(__name__)
debug_api_router = APIRouter()

# Re-export shared helpers from config_inspection so existing call sites and
# tests that imported them from this module keep working.
__all__ = [
    "SENSITIVE_FIELD_NAMES",
    "debug_api_router",
    "is_sensitive_field_name",
    "redact_sensitive_config",
]


def _strip_google_prefix(model: str) -> str:
    prefix = "models/"
    return model[len(prefix) :] if model.startswith(prefix) else model


def resolve_live_llm_model(llm_client: object) -> str | None:
    """Return the live primary model identifier from an LLM client.

    Provider clients disagree on where they stash the model identifier:

    - ``OpenAIClient`` / ``AnthropicClient`` set ``self.model``.
    - ``GoogleGenAIClient`` stores ``self.model_name`` with a leading
      ``models/`` prefix.
    - ``RetryingLLMClient`` (the wrapper created for profiles with
      ``processing_config.retry_config``) stores the active model on
      ``self.primary_model`` and does not itself have ``model`` /
      ``model_name`` attributes.

    We check these in priority order — wrapper's ``primary_model`` first so
    the dump reflects what the retry wrapper is actually driving — then fall
    through to the concrete provider attributes. The ``models/`` prefix is
    normalized away so consumers see the same identifier they configured.
    """
    raw_model = (
        getattr(llm_client, "primary_model", None)
        or getattr(llm_client, "model", None)
        or getattr(llm_client, "model_name", None)
    )
    if not isinstance(raw_model, str):
        return None
    return _strip_google_prefix(raw_model)


def resolve_live_llm_fallback_model(llm_client: object) -> str | None:
    """Return the configured fallback model for ``RetryingLLMClient``, if any.

    ``RetryingLLMClient.__init__`` sets ``self.fallback_model`` to a hard-coded
    default string even when the caller passes ``fallback_client=None``, so we
    MUST gate on the presence of a real ``fallback_client`` — otherwise every
    primary-only retry profile would falsely appear to have a fallback. We use
    ``hasattr`` + truthiness of ``fallback_client`` as the "fallback is actually
    wired" signal, and only then surface ``fallback_model``.

    Concrete provider clients (``OpenAIClient``, ``AnthropicClient``,
    ``GoogleGenAIClient``) do not expose a fallback chain at all, so this
    returns ``None`` for them.
    """
    if not hasattr(llm_client, "fallback_client"):
        return None
    if not getattr(llm_client, "fallback_client", None):
        return None
    fallback = getattr(llm_client, "fallback_model", None)
    if not isinstance(fallback, str):
        return None
    return _strip_google_prefix(fallback)


def _runtime_info_for(
    profile_id: str,
    # ast-grep-ignore: no-dict-any - Debug endpoint inspects dynamic runtime registry state
    processing_services_registry: dict[str, Any],
    # ast-grep-ignore: no-dict-any - Debug endpoint returns dynamic runtime introspection data
) -> dict[str, Any] | None:
    """Collect live runtime info for a profile from the services registry.

    The configured ``provider`` and ``llm_model`` are already in the profile's
    ``processing_config`` dump; we intentionally do NOT re-emit ``provider``
    here because no concrete LLM client exposes a stable ``provider`` attribute
    (it only appears on exception types). We do expose the live resolved model
    via :func:`resolve_live_llm_model` so callers can detect drift between the
    configured value and what the runtime client is actually using.
    """
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
        "llm_model": resolve_live_llm_model(llm_client) if llm_client else None,
        "llm_fallback_model": (
            resolve_live_llm_fallback_model(llm_client) if llm_client else None
        ),
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
    enablement). It is gated in three layers:

    1. ``Depends(get_current_user)`` — requires an OIDC session or API token
       when ``auth_service.auth_enabled`` is true.
    2. Fail-closed check — when auth is disabled, the endpoint only responds
       for deployments that have explicitly opted in by setting
       ``app_config.dev_mode=true``. This prevents misconfigured prod
       deployments (auth off, dev_mode off) from silently leaking prompts and
       policy rules, since ``get_current_user`` otherwise returns a synthetic
       test user in that configuration.
    3. Shared ``is_debug_authorized`` check used by the other
       ``/api/debug/*`` routes, so any future tightening of that gate applies
       here too.

    Secret-bearing fields (tokens, passwords, API keys) are redacted. The
    response defaults to pretty-printed JSON (``indent=2``); pass
    ``?format=raw`` for compact JSON.
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

    auth_service = getattr(app.state, "auth_service", None)
    auth_enabled = bool(auth_service and getattr(auth_service, "auth_enabled", False))
    dev_mode = bool(getattr(config, "dev_mode", False))
    if not auth_enabled and not dev_mode:
        # get_current_user() returned a synthetic test user because auth is
        # off. Refuse to expose profile internals unless the operator
        # explicitly set dev_mode=true.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Debug profile dump is disabled: authentication is not enabled "
                "and dev_mode is false. Enable OIDC auth or set "
                "app_config.dev_mode=true to access this endpoint."
            ),
        )

    processing_services_registry = getattr(app.state, "processing_services", None) or {}

    # ast-grep-ignore: no-dict-any - Debug endpoint returns serialized Pydantic config dicts
    profiles_info: list[dict[str, Any]] = []
    for profile in config.service_profiles:
        if profile_id is not None and profile.id != profile_id:
            continue
        profile_dump = redact_sensitive_config(dump_profile_like(profile))
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
        dump_profile_like(config.default_profile_settings)
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


async def _inventory_for_service(
    profile_id: str,
    service: object,
    *,
    can_confirm: bool,
) -> ToolInventory | None:
    """Build the tool inventory for one live processing service, if possible.

    Returns ``None`` when the service has no tools provider wired up (e.g. a
    delegation-only stub), so the caller can report it explicitly rather than
    silently dropping the profile.
    """
    tools_provider = getattr(service, "tools_provider", None)
    if tools_provider is None:
        return None
    on_demand_view = getattr(service, "on_demand_view", None)
    return await build_tool_inventory(
        tools_provider=tools_provider,
        on_demand_view=on_demand_view,
        can_confirm=can_confirm,
        profile_id=profile_id,
    )


@debug_api_router.get("/profiles/tools")
async def dump_profile_tool_inventory(  # noqa: A002 - FastAPI query param shadows builtin
    request: Request,
    _reader: Annotated[dict, Depends(get_diagnostics_reader)],
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
        Query(description="If set, return only the inventory for this profile."),
    ] = None,
    can_confirm: Annotated[
        bool,
        Query(
            description=(
                "Model the per-turn confirmation capability. When false, "
                "confirmation-gated tools the policy would drop are excluded, "
                "matching interactions that cannot prompt for confirmation."
            ),
        ),
    ] = True,
    include_tools: Annotated[
        bool,
        Query(
            description=(
                "Include the per-tool size list. Set false for summary-only "
                "totals (count + token estimate per profile)."
            ),
        ),
    ] = True,
) -> Response:
    """Dump the resolved per-profile tool advertisement for bloat analysis.

    For every live processing profile this reports the tools advertised to the
    LLM, partitioned into:

    - ``eager`` — advertised on **every** turn (the always-present tools plus
      the ``activate_tools`` meta-tool when on-demand tools exist). This is the
      headline ``advertised_per_turn_tokens`` figure and the main driver of
      tool bloat.
    - ``on_demand`` — hidden behind progressive disclosure until the model
      calls ``activate_tools`` (``tools_config.on_demand_local_tools`` /
      ``on_demand_mcp_server_ids``). These cost nothing until activated.

    Each tool carries a serialized size and a heuristic token estimate, and a
    ``by_source`` breakdown attributes the surface to ``local`` tools vs each
    ``mcp:<server_id>`` so you can see which MCP server is inflating the prompt.

    Unlike ``/profiles``, this exposes only tool names and sizes — no prompts or
    policy bodies — so it is gated by :func:`get_diagnostics_reader`, meaning the
    read-only ``DIAGNOSTICS_READONLY_TOKEN`` unlocks it for external monitors.

    The token figures are a heuristic (serialized JSON characters / 4) for
    relative comparison, not an exact provider token count.
    """
    app = request.app
    processing_services_registry = getattr(app.state, "processing_services", None) or {}

    matched_ids = sorted(processing_services_registry.keys())
    if profile_id is not None:
        if profile_id not in processing_services_registry:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Profile '{profile_id}' not found in the live service registry",
            )
        matched_ids = [profile_id]

    # ast-grep-ignore: no-dict-any - Debug endpoint returns serialized inventory dicts
    profiles_info: list[dict[str, Any]] = []
    for pid in matched_ids:
        service = processing_services_registry[pid]
        inventory = await _inventory_for_service(pid, service, can_confirm=can_confirm)
        if inventory is None:
            profiles_info.append({
                "profile_id": pid,
                "error": "No tools provider wired into this profile's service.",
            })
            continue
        inventory_dict = inventory.to_dict()
        if not include_tools:
            inventory_dict["eager"].pop("tools", None)
            inventory_dict["on_demand"].pop("tools", None)
        profiles_info.append(inventory_dict)

    # ast-grep-ignore: no-dict-any - Debug endpoint returns serialized inventory dicts
    payload: dict[str, Any] = {
        "can_confirm": can_confirm,
        "token_estimate_note": (
            "estimated_tokens is a heuristic (serialized JSON characters / 4) "
            "for relative comparison, not an exact provider token count."
        ),
        "profiles": profiles_info,
        "profile_count": len(profiles_info),
        "registered_service_ids": sorted(processing_services_registry.keys()),
    }

    indent = None if format == "raw" else 2
    return Response(
        content=json.dumps(payload, indent=indent, sort_keys=False, default=str),
        media_type="application/json",
    )
