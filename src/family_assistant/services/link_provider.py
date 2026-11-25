"""The Stripe Link Agent Wallet instance of the generic OAuth-provider layer."""

from __future__ import annotations

from enum import StrEnum

import httpx

from family_assistant.services.oauth_provider import OAuthProviderSpec

LINK_DEFAULT_OAUTH_CLIENT_ID = "lwlpk_U7Qy7ThG69STZk"
LINK_API_BASE_URL = "https://api.link.com"


class LinkScope(StrEnum):
    """OAuth scopes the shipped Link Agent Wallet tools can exercise."""

    PAYMENT_METHODS_AGENTIC = "payment_methods.agentic"
    USERINFO_READ = "userinfo:read"


def is_link_terminal_refresh_error(response: httpx.Response) -> bool:
    """Classify Link's terminal invalid/redeemed/revoked refresh responses.

    Link's ``/device/token`` returns HTTP 400 with ``{"error": {"message": ...}}``
    (e.g. ``"refresh_token is not active: status: redeemed"``) rather than
    RFC 6749's ``{"error": "invalid_grant"}``. Accept both shapes on 400 while
    keeping 5xx, 429, and non-JSON errors transient.
    """
    if response.status_code != httpx.codes.BAD_REQUEST:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    if error == "invalid_grant":
        return True
    if isinstance(error, dict):
        message = error.get("message")
        return isinstance(message, str) and bool(message.strip())
    return False


LINK_PROVIDER = OAuthProviderSpec(
    name="link",
    display_name="Link",
    config_attr="link_integration",
    authorize_url=None,
    device_code_url="https://login.link.com/device/code",
    token_url="https://login.link.com/device/token",
    revoke_url="https://login.link.com/device/revoke",
    userinfo_url=f"{LINK_API_BASE_URL}/userinfo",
    supported_scopes=frozenset(scope.value for scope in LinkScope),
    identity_scopes=(),
    extra_authorize_params={},
    requires_client_secret=False,
    requires_taint_floor=False,
    is_terminal_refresh_error=is_link_terminal_refresh_error,
)
