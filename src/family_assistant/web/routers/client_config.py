"""Router for client configuration."""

import os

from fastapi import APIRouter

router = APIRouter()


@router.get("/api/client_config")
async def get_client_config() -> dict[str, str | None]:
    """Get client-side configuration.

    Returns:
        Dictionary containing client configuration like VAPID public key
    """
    return {
        "vapidPublicKey": os.getenv("VAPID_PUBLIC_KEY"),
    }
