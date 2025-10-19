"""Router for push notification subscriptions."""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import exc as sqlalchemy_exc

from family_assistant.storage.context import DatabaseContext
from family_assistant.web.dependencies import get_current_user, get_db

router = APIRouter()
logger = logging.getLogger(__name__)


class PushSubscriptionKeys(BaseModel):
    """Encryption keys for push subscription."""

    p256dh: str
    auth: str


class PushSubscriptionData(BaseModel):
    """Typed push subscription data from client."""

    endpoint: str
    keys: PushSubscriptionKeys
    expirationTime: int | None = None


class PushSubscriptionRequest(BaseModel):
    """Request model for push subscription."""

    subscription: PushSubscriptionData


class UnsubscribeRequest(BaseModel):
    """Request model for unsubscribe."""

    endpoint: str


@router.post("/api/push/subscribe")
async def subscribe(
    request: PushSubscriptionRequest,
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    user: Annotated[dict[str, Any], Depends(get_current_user)],
    db: Annotated[DatabaseContext, Depends(get_db)],
) -> dict[str, str]:
    """Subscribe to push notifications.

    Args:
        request: Push subscription request containing subscription data
        user: Current authenticated user
        db: Database context

    Returns:
        Success response with subscription ID
    """
    try:
        subscription_id = await db.push_subscriptions.add(
            user_identifier=user["user_identifier"],
            subscription_json=request.subscription.model_dump(exclude_none=True),
        )
        logger.info(
            f"Created push subscription {subscription_id} for user {user['user_identifier']}"
        )
        return {"status": "success", "id": str(subscription_id)}
    except sqlalchemy_exc.SQLAlchemyError as e:
        logger.error(f"Database error creating subscription: {e}")
        raise HTTPException(
            status_code=503, detail="Database error creating subscription"
        ) from e
    except Exception as e:
        logger.error(f"Failed to create subscription: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to create subscription"
        ) from e


@router.post("/api/push/unsubscribe")
async def unsubscribe(
    request: UnsubscribeRequest,
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    user: Annotated[dict[str, Any], Depends(get_current_user)],
    db: Annotated[DatabaseContext, Depends(get_db)],
) -> dict[str, str]:
    """Unsubscribe from push notifications.

    Args:
        request: Unsubscribe request with endpoint URL
        user: Current authenticated user
        db: Database context

    Returns:
        Success response with status
    """
    deleted_count = await db.push_subscriptions.delete_by_endpoint(
        user_identifier=user["user_identifier"], endpoint=request.endpoint
    )
    if deleted_count > 0:
        logger.info(
            f"Deleted {deleted_count} subscriptions for user {user['user_identifier']} "
            f"with endpoint {request.endpoint}"
        )
        return {"status": "success"}
    return {"status": "not_found"}
