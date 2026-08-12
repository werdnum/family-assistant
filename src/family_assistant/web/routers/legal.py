"""Public legal pages.

These pages must be reachable without authentication: app store review teams
(and anyone evaluating the app before signing in) need to read the privacy
policy, so the paths served here are listed in
:data:`family_assistant.web.auth.PUBLIC_PATHS`.
"""

import logging
import os
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)
legal_router = APIRouter()

PRIVACY_POLICY_LAST_UPDATED = "12 August 2026"
DEFAULT_OPERATOR = "the person who operates this Family Assistant instance"


def get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


@legal_router.get("/privacy", response_class=HTMLResponse, name="privacy_policy")
async def privacy_policy(
    request: Request,
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
) -> HTMLResponse:
    """Serve the privacy policy as a standalone, unauthenticated page."""
    return templates.TemplateResponse(
        request,
        "privacy_policy.html.j2",
        context={
            "operator": os.getenv("PRIVACY_POLICY_OPERATOR") or DEFAULT_OPERATOR,
            "contact_email": os.getenv("PRIVACY_POLICY_CONTACT_EMAIL"),
            "last_updated": PRIVACY_POLICY_LAST_UPDATED,
        },
    )
