"""The consent page an MCP client's user approves or denies the connection on.

An ordinary authenticated page, not an API route: with OIDC configured,
``AuthMiddleware`` sends a signed-out user through login and back here. It is
server-rendered because it is one form with two buttons reached only mid
OAuth flow (docs/design/mcp-adapter.md, "Deliberate simplifications").
"""

import html
import logging
import secrets
from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from family_assistant.web.dependencies import get_current_user
from family_assistant.web.mcp_adapter.config import (
    builtin_authorization_server_enabled,
)
from family_assistant.web.mcp_adapter.oauth.provider import (
    CONSENT_PATH,
    PendingConsent,
    StoreFullError,
)
from family_assistant.web.mcp_adapter.oauth.routes import oauth_server

logger = logging.getLogger(__name__)

CONSENT_ROUTE_NAME = "mcp_consent"
CSRF_SESSION_KEY = "mcp_consent_nonces"
# Consent flows one browser may hold open at once; older nonces are dropped.
MAX_SESSION_NONCES = 10

consent_router = APIRouter()


def _session_nonce(request: Request, request_id: str, nonce: str | None) -> str | None:
    """Store ``nonce`` for ``request_id`` in the session, or None without sessions.

    Without ``SessionMiddleware`` there is no cookie-based identity for a
    cross-site form post to ride on, so there is nothing for the nonce to
    protect; a session that exists must carry it. Nonces are keyed by consent
    request so two flows open in the same browser do not invalidate each other;
    with ``nonce`` None the matching entry is consumed.
    """
    try:
        session = request.session
    except AssertionError:
        return None
    nonces: dict[str, str] = dict(session.get(CSRF_SESSION_KEY) or {})
    if nonce is None:
        expected = nonces.pop(request_id, "")
    else:
        nonces[request_id] = nonce
        for stale in list(nonces)[: max(0, len(nonces) - MAX_SESSION_NONCES)]:
            del nonces[stale]
        expected = nonce
    session[CSRF_SESSION_KEY] = nonces
    return expected


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
        content=f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title)}</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 32rem; margin: 3rem auto; padding: 0 1rem; color: #222; }}
h1 {{ font-size: 1.4rem; }}
.client {{ font-weight: 600; }}
.buttons {{ display: flex; gap: 1rem; margin-top: 2rem; }}
button {{ font: inherit; padding: 0.6rem 1.4rem; border-radius: 6px; border: 1px solid #888; background: #f4f4f4; cursor: pointer; }}
button.approve {{ background: #2563eb; border-color: #2563eb; color: #fff; }}
</style>
</head>
<body>
{body}
</body>
</html>""",
    )


def _consent_form(pending: PendingConsent, nonce: str | None) -> str:
    client_name = html.escape(pending.client.client_name or pending.client.client_id)
    redirect_host = html.escape(urlparse(str(pending.params.redirect_uri)).netloc)
    nonce_field = (
        f'<input type="hidden" name="nonce" value="{html.escape(nonce)}">'
        if nonce
        else ""
    )
    return f"""<h1>Connect to Family Assistant?</h1>
<p><span class="client">{client_name}</span> (<code>{redirect_host}</code>) is asking to talk to
your Family Assistant. If you approve, it can ask the assistant questions on your behalf,
as you, until you revoke its token from the API tokens page.</p>
<form method="post" action="{CONSENT_PATH}">
<input type="hidden" name="request_id" value="{html.escape(pending.request_id)}">
{nonce_field}
<div class="buttons">
<button type="submit" name="decision" value="approve" class="approve">Approve</button>
<button type="submit" name="decision" value="deny">Deny</button>
</div>
</form>"""


def _expired() -> HTMLResponse:
    return _page(
        "Request expired",
        "<h1>This connection request has expired</h1>"
        "<p>Go back to the application that sent you here and start connecting again.</p>",
        status_code=status.HTTP_400_BAD_REQUEST,
    )


async def consent_user(request: Request) -> dict:
    """Authenticate the consent page, unless the authorization server is off.

    Enablement is checked before authentication so a disabled adapter answers
    404 for a signed-out visitor too, rather than a 401 or a login redirect
    from a dependency that would otherwise run first. An external authorization
    server replaces the page along with the rest of the built-in one.
    """
    if not builtin_authorization_server_enabled(request.app):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="MCP authorization server is not enabled.",
        )
    return await get_current_user(request)


@consent_router.get(CONSENT_PATH, name=CONSENT_ROUTE_NAME, include_in_schema=False)
async def consent_page(
    request: Request,
    request_id: str,
    current_user: Annotated[dict, Depends(consent_user)],
) -> HTMLResponse:
    """Show who is asking and what approving grants."""
    del current_user  # Authentication is the point; the decision names the user.
    pending = oauth_server(request.app).provider.pending_consent(request_id)
    if pending is None:
        return _expired()
    nonce = _session_nonce(request, request_id, secrets.token_urlsafe(16))
    return _page("Connect to Family Assistant", _consent_form(pending, nonce))


@consent_router.post(CONSENT_PATH, include_in_schema=False)
async def consent_decision(
    request: Request,
    current_user: Annotated[dict, Depends(consent_user)],
    request_id: Annotated[str, Form()],
    decision: Annotated[str, Form()],
    nonce: Annotated[str | None, Form()] = None,
) -> Response:
    """Turn the user's decision into the client's redirect."""
    expected_nonce = _session_nonce(request, request_id, None)
    if expected_nonce is not None and not (
        nonce and secrets.compare_digest(expected_nonce, nonce)
    ):
        return _page(
            "Request rejected",
            "<h1>This form was not submitted from the consent page</h1>"
            "<p>Go back to the application that sent you here and start connecting again.</p>",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    provider = oauth_server(request.app).provider
    if decision == "approve":
        try:
            redirect = provider.approve_consent(
                request_id, current_user["user_identifier"]
            )
        except StoreFullError:
            return _page(
                "Try again shortly",
                "<h1>Too many connections are being set up right now</h1>"
                "<p>Go back to the application that sent you here and try again in a few minutes.</p>",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
    else:
        redirect = provider.deny_consent(request_id)
    if redirect is None:
        return _expired()
    return RedirectResponse(
        redirect,
        status_code=status.HTTP_302_FOUND,
        headers={"Cache-Control": "no-store"},
    )
