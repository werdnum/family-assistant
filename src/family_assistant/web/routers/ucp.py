"""Universal Commerce Protocol public profile endpoint."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from family_assistant.config_models import AppConfig
from family_assistant.services.ucp import UCPConfigurationError, build_ucp_profile

router = APIRouter()


@router.get("/.well-known/ucp", include_in_schema=False, response_model=None)
async def get_ucp_profile(request: Request) -> JSONResponse:
    """Serve the public UCP platform profile."""
    app_config = getattr(request.app.state, "config", None)
    if not isinstance(app_config, AppConfig):
        raise HTTPException(status_code=503, detail="Application config unavailable")

    try:
        profile = build_ucp_profile(app_config)
    except UCPConfigurationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    max_age = app_config.ucp_config.profile_cache_max_age_seconds
    return JSONResponse(
        profile,
        headers={"Cache-Control": f"public, max-age={max_age}"},
    )
