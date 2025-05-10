import logging
from datetime import datetime, timezone  # Added datetime and timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from family_assistant.storage import api_tokens as api_tokens_storage
from family_assistant.storage.context import DatabaseContext
from family_assistant.web.dependencies import get_current_active_user, get_db

logger = logging.getLogger(__name__)

# This router will be included with a prefix like /settings/tokens
# So paths here are relative to that.
router = APIRouter()


@router.get("", response_class=HTMLResponse, name="ui_manage_api_tokens")
async def manage_api_tokens_ui(
    request: Request,
    current_user: dict = Depends(get_current_active_user),
    db_context: DatabaseContext = Depends(get_db),
):
    """
    Displays the API token management page.
    Lists existing tokens and provides a form to create new ones.
    """
    user_identifier = current_user.get("sub")
    if not user_identifier:
        logger.error(
            "User 'sub' (identifier) not found in session for UI token management. User data: %s",
            current_user,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="User identifier not found in session.",
        )

    try:
        tokens = await api_tokens_storage.get_api_tokens_for_user(
            db_context, user_identifier
        )
    except Exception as e:
        logger.error(
            "Failed to fetch API tokens for user %s: %s", user_identifier, e, exc_info=True
        )
        # Render the page with an error message or an empty list
        tokens = []
        # Optionally, add an error message to the context to display in the template
        # context["error_message"] = "Could not load your API tokens."

    context = {
        "request": request,
        "user": current_user,
        "tokens": tokens,
        "page_title": "Manage API Tokens",
        "server_url": request.app.state.server_url, # For constructing API call URLs in JS
        "now_utc": datetime.now(timezone.utc), # Add current UTC time for template logic
    }
    return request.app.state.templates.TemplateResponse(
        "settings/api_tokens.html", context
    )


@router.post("/revoke/{token_id}", name="ui_revoke_api_token")
async def revoke_api_token_ui(
    request: Request, # For redirecting
    token_id: int,
    current_user: dict = Depends(get_current_active_user),
    db_context: DatabaseContext = Depends(get_db),
    # CSRF token could be added here as a Form field for better security
):
    """
    Handles the revocation of an API token via a form POST.
    """
    user_identifier = current_user.get("sub")
    if not user_identifier:
        logger.error(
            "User 'sub' (identifier) not found in session for token revocation. User data: %s",
            current_user,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="User identifier not found in session.",
        )

    try:
        success = await api_tokens_storage.revoke_api_token(
            db_context, token_id, user_identifier
        )
        if success:
            logger.info(
                "Token ID %s successfully revoked by user %s via UI.",
                token_id,
                user_identifier,
            )
            # Optionally, add a success message to the session/flash message system
            # request.session["flash_message"] = "Token revoked successfully."
        else:
            logger.warning(
                "Failed to revoke token ID %s by user %s via UI (not found or not owned).",
                token_id,
                user_identifier,
            )
            # request.session["flash_message"] = "Failed to revoke token: Not found or not owned by you."
    except Exception as e:
        logger.error(
            "Error revoking token ID %s for user %s via UI: %s",
            token_id,
            user_identifier,
            e,
            exc_info=True,
        )
        # request.session["flash_message"] = "An error occurred while revoking the token."

    # Redirect back to the token management page
    return RedirectResponse(
        url=request.url_for("ui_manage_api_tokens"), status_code=status.HTTP_303_SEE_OTHER
    )
