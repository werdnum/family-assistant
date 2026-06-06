"""Router for iOS APNs device token registration."""

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import exc as sqlalchemy_exc

from family_assistant.storage.context import DatabaseContext
from family_assistant.web.auth import User
from family_assistant.web.dependencies import get_current_user, get_db

router = APIRouter()
logger = logging.getLogger(__name__)


class IosPushTokenRequest(BaseModel):
    """Request model for registering an iOS APNs device token.

    The payload key matches the iOS client, which posts ``token``.
    """

    token: str
    environment: Literal["production", "sandbox"] = "production"
    bundle_id: str | None = None


@router.post("/api/ios/push-tokens")
async def register_token(
    request: IosPushTokenRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[DatabaseContext, Depends(get_db)],
) -> dict[str, str]:
    """Register (or refresh) an iOS APNs device token for the current user.

    Args:
        request: Token registration request.
        user: Current authenticated user.
        db: Database context.

    Returns:
        Success response with the stored token row id.
    """
    try:
        token_id = await db.ios_push_tokens.upsert(
            user_identifier=user["user_identifier"],
            device_token=request.token,
            environment=request.environment,
            bundle_id=request.bundle_id,
        )
        logger.info(
            "Registered iOS push token %s for user %s",
            token_id,
            user["user_identifier"],
        )
        return {"status": "success", "id": str(token_id)}
    except sqlalchemy_exc.SQLAlchemyError as e:
        logger.error(f"Database error registering iOS push token: {e}")
        raise HTTPException(
            status_code=503, detail="Database error registering push token"
        ) from e


@router.delete("/api/ios/push-tokens/{token}")
async def unregister_token(
    token: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[DatabaseContext, Depends(get_db)],
) -> dict[str, str]:
    """Unregister an iOS APNs device token belonging to the current user.

    Args:
        token: The APNs device token to remove.
        user: Current authenticated user.
        db: Database context.

    Returns:
        Success response with status.
    """
    deleted_count = await db.ios_push_tokens.delete_for_user(
        user_identifier=user["user_identifier"], device_token=token
    )
    if deleted_count > 0:
        logger.info(
            "Deleted iOS push token for user %s",
            user["user_identifier"],
        )
        return {"status": "success"}
    return {"status": "not_found"}
