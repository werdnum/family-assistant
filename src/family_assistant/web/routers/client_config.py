"""Router for client configuration."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from family_assistant.web.dependencies import get_current_user

router = APIRouter()


@router.get("/api/client_config")
async def get_client_config(
    request: Request,
    user: Annotated[dict, Depends(get_current_user)],
) -> dict[str, str | None]:
    """Get client-side configuration.

    Returns:
        Dictionary containing client configuration like VAPID public key
    """
    vapid_public_key: str | None = None
    app_config = getattr(request.app.state, "config", None)
    if app_config:
        if hasattr(app_config, "pwa_config") and hasattr(
            app_config.pwa_config, "vapid_public_key"
        ):
            vapid_public_key = app_config.pwa_config.vapid_public_key
        elif isinstance(app_config, dict):
            pwa_config = app_config.get("pwa_config", {})
            if isinstance(pwa_config, dict):
                vapid_public_key = pwa_config.get("vapid_public_key")
    return {
        "vapidPublicKey": vapid_public_key,
    }
