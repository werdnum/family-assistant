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
    pwa_config = request.app.state.config.get("pwa_config", {})
    return {
        "vapidPublicKey": pwa_config.get("vapid_public_key"),
    }
