"""Web endpoints for the deployment's ChatGPT plan sign-in.

Session-only, like the Google connect flow: an API token cannot start a
sign-in, finish one, or sign the deployment out. Mounted at
``/api/integrations/chatgpt`` (see ``routers/api.py``):

- ``GET    /``         -- sign-in status (never any token material).
- ``POST   /sign-in``  -- start an attempt; returns the URL to open.
- ``POST   /callback`` -- finish it with the loopback address the browser
  was sent to, pasted by the user.
- ``DELETE /``         -- revoke and clear the sign-in.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from family_assistant.services.chatgpt_plan import (
    REDIRECT_URI,
    USAGE_SETTINGS_URL,
    ChatGPTPlanAuth,
    ChatGPTPlanSignInError,
)
from family_assistant.web.dependencies import get_current_session_user

logger = logging.getLogger(__name__)

chatgpt_plan_api_router = APIRouter()

CurrentUser = Annotated[dict, Depends(get_current_session_user)]


class ChatGPTPlanStatusResponse(BaseModel):
    enabled: bool
    status: str | None
    email: str | None
    connected_by_user_id: str | None
    last_used_at: str | None
    redirect_uri: str
    usage_url: str


class StartSignInRequest(BaseModel):
    # Sign the stored account in again rather than registering a new one.
    use_saved_account: bool = True


class StartSignInResponse(BaseModel):
    authorize_url: str


class CompleteSignInRequest(BaseModel):
    callback_url: str


class SignOutResponse(BaseModel):
    revocation_confirmed: bool


def _auth(request: Request) -> ChatGPTPlanAuth | None:
    auth = getattr(request.app.state, "chatgpt_plan", None)
    return auth if isinstance(auth, ChatGPTPlanAuth) else None


def _require_auth(request: Request) -> ChatGPTPlanAuth:
    auth = _auth(request)
    if auth is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="ChatGPT sign-in is not available: CREDENTIAL_ENCRYPTION_KEY is "
            "not set.",
        )
    return auth


@chatgpt_plan_api_router.get("")
async def get_status(
    request: Request, current_user: CurrentUser
) -> ChatGPTPlanStatusResponse:
    del current_user
    auth = _auth(request)
    view = await auth.status() if auth is not None else None
    return ChatGPTPlanStatusResponse(
        enabled=auth is not None,
        status=view.status if view else None,
        email=view.email if view else None,
        connected_by_user_id=view.connected_by_user_id if view else None,
        last_used_at=(
            view.last_used_at.isoformat() if view and view.last_used_at else None
        ),
        redirect_uri=REDIRECT_URI,
        usage_url=USAGE_SETTINGS_URL,
    )


@chatgpt_plan_api_router.post("/sign-in")
async def start_sign_in(
    request: Request, current_user: CurrentUser, body: StartSignInRequest
) -> StartSignInResponse:
    auth = _require_auth(request)
    url = await auth.start_sign_in(
        current_user["user_identifier"], use_saved_account=body.use_saved_account
    )
    return StartSignInResponse(authorize_url=url)


@chatgpt_plan_api_router.post("/callback")
async def complete_sign_in(
    request: Request, current_user: CurrentUser, body: CompleteSignInRequest
) -> ChatGPTPlanStatusResponse:
    auth = _require_auth(request)
    try:
        await auth.complete_sign_in(current_user["user_identifier"], body.callback_url)
    except ChatGPTPlanSignInError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("ChatGPT plan signed in by %s", current_user["user_identifier"])
    return await get_status(request, current_user)


@chatgpt_plan_api_router.delete("")
async def sign_out(request: Request, current_user: CurrentUser) -> SignOutResponse:
    auth = _require_auth(request)
    confirmed = await auth.sign_out()
    logger.info(
        "ChatGPT plan signed out by %s (revocation confirmed: %s)",
        current_user["user_identifier"],
        confirmed,
    )
    return SignOutResponse(revocation_confirmed=confirmed)
