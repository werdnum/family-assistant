import logging
from datetime import datetime, timezone
from typing import Annotated

from dateutil import parser as dateutil_parser
from fastapi import APIRouter, Depends, HTTPException, status

from family_assistant.storage import api_tokens as api_tokens_storage
from family_assistant.storage.context import DatabaseContext
from family_assistant.web.dependencies import get_current_active_user, get_db
from family_assistant.web.models import (
    ApiTokenCreateRequest,
    ApiTokenCreateResponse,
)

logger = logging.getLogger(__name__)

# This router will be included with a prefix like /api/me/tokens
# So paths here are relative to that.
router = APIRouter()


@router.post(
    "",  # Path will be relative to the router's prefix
    response_model=ApiTokenCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new API token for the authenticated user",
)
async def create_api_token(
    token_data: ApiTokenCreateRequest,
    current_user: Annotated[dict, Depends(get_current_active_user)],
    db_context: Annotated[DatabaseContext, Depends(get_db)],
) -> ApiTokenCreateResponse:
    """
    Creates a new API token for the currently authenticated user (via OIDC).
    The full token is returned only once upon creation.
    """
    user_identifier = current_user.get("sub")  # 'sub' is standard OIDC subject claim
    if not user_identifier:
        logger.error(
            "User 'sub' (identifier) not found in session for token creation. User data: %s",
            current_user,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="User identifier not found in session.",
        )

    expires_at_dt: datetime | None = None
    if token_data.expires_at:
        try:
            expires_at_dt = dateutil_parser.isoparse(token_data.expires_at)
            if expires_at_dt.tzinfo is None:  # Ensure timezone aware
                expires_at_dt = expires_at_dt.replace(tzinfo=timezone.utc)
            if expires_at_dt <= datetime.now(timezone.utc):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Expiration date must be in the future.",
                )
        except ValueError as e:
            logger.warning(
                "Invalid expires_at format '%s' for user %s: %s",
                token_data.expires_at,
                user_identifier,
                e,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid 'expires_at' format: {e}. Use ISO 8601 format.",
            ) from e

    try:
        full_token, token_id, created_at_utc = (
            await api_tokens_storage.create_and_store_api_token(
                db_context=db_context,
                user_identifier=user_identifier,
                name=token_data.name,
                expires_at=expires_at_dt,
            )
        )
        logger.info(
            "API Token ID %s created for user %s (Name: %s)",
            token_id,
            user_identifier,
            token_data.name,
        )

        # Construct the response. Note that some fields like is_revoked and last_used_at
        # are defaults for a new token.
        return ApiTokenCreateResponse(
            id=token_id,
            name=token_data.name,
            full_token=full_token,
            prefix=full_token[: api_tokens_storage.TOKEN_PREFIX_LENGTH],
            user_identifier=user_identifier,
            created_at=created_at_utc,
            expires_at=expires_at_dt,
            is_revoked=False,  # New tokens are not revoked
            last_used_at=None,  # New tokens haven't been used
        )

    except Exception as e:
        logger.error(
            "Failed to create API token for user %s (Name: %s): %s",
            user_identifier,
            token_data.name,
            e,
            exc_info=True,
        )
        # Check for specific database errors if needed, e.g., unique constraint on prefix
        # For now, a generic 500 error.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create API token.",
        ) from e
