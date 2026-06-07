from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from family_assistant.web.dependencies import get_current_user

me_router = APIRouter()


@me_router.get("/me", summary="Return the authenticated application user")
async def get_me(
    current_user: Annotated[dict[str, object], Depends(get_current_user)],
) -> dict[str, object | None]:
    """Return non-sensitive identity diagnostics for the current authenticated user."""
    return {
        "user_identifier": current_user.get("user_identifier"),
        "user_label": current_user.get("user_label"),
        "raw_user_identifier": current_user.get("raw_user_identifier"),
        "identity_source": current_user.get("identity_source"),
        "identity_source_identifier": current_user.get("identity_source_identifier"),
        "email": current_user.get("email"),
        "sub": current_user.get("sub"),
        "source": current_user.get("source"),
    }
