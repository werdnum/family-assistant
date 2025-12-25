"""Router for client configuration."""

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Request

from family_assistant.web.dependencies import get_current_user

if TYPE_CHECKING:
    from family_assistant.config_models import AppConfig

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
    app_config: AppConfig | None = getattr(request.app.state, "config", None)
    if app_config:
        vapid_public_key = app_config.pwa_config.vapid_public_key
    return {
        "vapidPublicKey": vapid_public_key,
    }
