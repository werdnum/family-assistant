"""Authenticated Google REST requests on behalf of one acting user.

The Gmail/Drive tools and Google Calendar all issue requests the same way: resolve
the acting user's access token, call the shared :class:`ApiBackend`, retry once on
a ``401`` after evicting the cached token, and turn any other non-2xx status into
a concise, token-free :class:`GoogleApiError`. This module is that one path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from family_assistant.services.api_backend import ApiBackendError
from family_assistant.services.google_provider import GOOGLE_PROVIDER

if TYPE_CHECKING:
    from collections.abc import Mapping

    from family_assistant.services.api_backend import ApiBackend, ApiResponse
    from family_assistant.services.google_provider import GoogleScope
    from family_assistant.services.oauth_credentials import OAuthCredentialResolver
    from family_assistant.storage.database import Database
    from family_assistant.tools.types import ToolExecutionContext


class GoogleApiError(Exception):
    """A non-credential Google API failure, safe to show the user."""


@dataclass(frozen=True)
class GoogleUserApi:
    """Google REST access bound to one acting user.

    Built from a tool's execution context, or by a per-turn context provider
    from the turn's acting user. Nothing model-supplied chooses ``user_id``.
    """

    resolver: OAuthCredentialResolver
    backend: ApiBackend
    db: Database
    user_id: str

    @classmethod
    def from_exec_context(
        cls, exec_context: ToolExecutionContext
    ) -> GoogleUserApi | None:
        """Bind to the tool call's acting user, or None when Google is off.

        ``None`` means the integration is not enabled for this deployment or the
        turn has no acting user; callers decide whether that is an error.
        """
        resolver = (exec_context.credential_resolvers or {}).get(GOOGLE_PROVIDER.name)
        backend = exec_context.api_backend
        if resolver is None or backend is None or not exec_context.user_id:
            return None
        return cls(
            resolver=resolver,
            backend=backend,
            db=exec_context.db_context,
            user_id=exec_context.user_id,
        )

    def scope_configured(self, scope: GoogleScope) -> bool:
        """Whether this deployment requests ``scope`` at consent."""
        return scope.value in self.resolver.configured_scopes

    async def request(
        self,
        scope: GoogleScope,
        *,
        method: str = "GET",
        url: str,
        params: Mapping[str, str] | None = None,
        content: bytes | None = None,
        content_type: str | None = None,
    ) -> ApiResponse:
        """Issue an authenticated request, retrying once on ``401``.

        A ``401`` means the token was revoked before its cached expiry: the
        cached token is evicted and the request retried with a fresh one. If
        that forced refresh fails with ``invalid_grant`` the resolver raises
        ``OAuthReauthRequiredError``. Any other non-2xx status raises
        :class:`GoogleApiError`.
        """
        access_token = await self.resolver.access_token_for_user(
            self.db, self.user_id, scope
        )
        response = await self._send(
            method=method,
            url=url,
            access_token=access_token,
            params=params,
            content=content,
            content_type=content_type,
        )
        if response.status_code == 401:
            self.resolver.evict_cached_token(self.user_id)
            access_token = await self.resolver.access_token_for_user(
                self.db, self.user_id, scope
            )
            response = await self._send(
                method=method,
                url=url,
                access_token=access_token,
                params=params,
                content=content,
                content_type=content_type,
            )

        if 200 <= response.status_code < 300:
            await self.db.oauth_connections.update_last_used(
                self.user_id, GOOGLE_PROVIDER.name
            )
            return response
        raise GoogleApiError(format_google_api_error(response))

    async def _send(
        self,
        *,
        method: str,
        url: str,
        access_token: str,
        params: Mapping[str, str] | None,
        content: bytes | None,
        content_type: str | None,
    ) -> ApiResponse:
        """Call the shared backend, naming the provider in transport errors.

        The backend is provider-neutral and shared, so its transport/oversize
        messages carry no provider name; these errors deliberately propagate past
        the tool boundary to the generic tool-error renderer, where the user must
        still see which provider failed ("Google API request to ... failed").
        """
        try:
            return await self.backend.request(
                method=method,
                url=url,
                access_token=access_token,
                params=params,
                content=content,
                content_type=content_type,
            )
        except ApiBackendError as exc:
            raise ApiBackendError(f"{GOOGLE_PROVIDER.display_name} {exc}") from exc


def format_google_api_error(response: ApiResponse) -> str:
    """Build a concise, token-free error message from a non-2xx response."""
    detail = ""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                detail = message
        elif isinstance(error, str):
            detail = error
    suffix = f": {detail}" if detail else ""
    return f"Google API request failed (HTTP {response.status_code}){suffix}"
