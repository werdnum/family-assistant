"""Turn on web authentication for the workhorse sandbox.

``family_assistant.web.auth`` reads its configuration from the environment
once, when it is first imported, and much of the application imports it
indirectly. This module imports nothing from the application, so a runner
can call :func:`apply_sandbox_auth_environment` before anything else.
"""

from __future__ import annotations

import os
import sys

__all__ = ["apply_sandbox_auth_environment", "require_sandbox_auth"]

_AUTH_ENVIRONMENT = {
    "OIDC_CLIENT_ID": "workhorse-eval",
    "OIDC_CLIENT_SECRET": "workhorse-eval-secret",
    # Discovery is fetched only for an interactive login, which the sandbox
    # never performs; requests authenticate with an API token instead.
    "OIDC_DISCOVERY_URL": "http://127.0.0.1:9/.well-known/openid-configuration",
    "SESSION_SECRET_KEY": "workhorse-eval-session-secret-0123456789",
}


def apply_sandbox_auth_environment() -> None:
    """Turn on web authentication; call before importing the application."""
    if "family_assistant.web.auth" in sys.modules:
        raise RuntimeError(
            "family_assistant.web.auth was imported before the sandbox's auth "
            "environment was applied, so authentication would stay off. Call "
            "apply_sandbox_auth_environment() first."
        )
    for name, value in _AUTH_ENVIRONMENT.items():
        os.environ.setdefault(name, value)


def require_sandbox_auth() -> None:
    """Fail unless web authentication came up enabled."""
    auth = sys.modules.get("family_assistant.web.auth")
    if auth is None or not getattr(auth, "AUTH_ENABLED", False):
        raise RuntimeError(
            "The workhorse sandbox needs web authentication. Call "
            "apply_sandbox_auth_environment() before importing the application."
        )
