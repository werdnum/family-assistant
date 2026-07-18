"""The Google (Gmail/Drive) instance of the generic OAuth-provider layer."""

from __future__ import annotations

from enum import StrEnum

from family_assistant.services.oauth_provider import OAuthProviderSpec


class GoogleScope(StrEnum):
    """OAuth scopes the shipped Google tools can exercise (read-only)."""

    GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
    DRIVE_READONLY = "https://www.googleapis.com/auth/drive.readonly"
    DRIVE_METADATA_READONLY = "https://www.googleapis.com/auth/drive.metadata.readonly"


GOOGLE_PROVIDER = OAuthProviderSpec(
    name="google",
    display_name="Google",
    config_attr="google_integration",
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    token_url="https://oauth2.googleapis.com/token",
    revoke_url="https://oauth2.googleapis.com/revoke",
    userinfo_url="https://www.googleapis.com/oauth2/v3/userinfo",
    supported_scopes=frozenset(scope.value for scope in GoogleScope),
    identity_scopes=("openid", "email"),
    extra_authorize_params={"access_type": "offline", "prompt": "consent"},
)
