"""Access tokens an external authorization server issues for the MCP endpoint.

With ``mcp_adapter.authorization_server`` configured, MCP clients sign in with
that server rather than with the adapter's own, and present its signed access
tokens at ``/api/mcp``. An edge gateway verifies them before the request gets
here; this module verifies them again so the endpoint is not open to anything
that reaches the application without passing the gateway, and turns the claims
into the user the tool runs as. See docs/design/mcp-adapter.md.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import jwt
from starlette.datastructures import State

from family_assistant.config_models import MCPExternalAuthorizationServer

logger = logging.getLogger(__name__)

# Asymmetric algorithms only: a symmetric one would let anyone holding the
# verification key mint tokens.
ALLOWED_ALGORITHMS = [
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
]
JWKS_CACHE_SECONDS = 300
# A token naming a key the cached set lacks refetches the set at most this
# often, so a stream of made-up key IDs cannot turn every request into a fetch
# while a rotated-in key is still picked up.
UNKNOWN_KEY_REFRESH_SECONDS = 60


@dataclass
class ExternalTokenVerifier:
    """Verifies one issuer's tokens against its published signing keys."""

    server: MCPExternalAuthorizationServer
    jwks: jwt.PyJWKClient
    last_unknown_key_refresh: float = field(default=float("-inf"), init=False)

    @classmethod
    def for_server(
        cls, server: MCPExternalAuthorizationServer
    ) -> "ExternalTokenVerifier":
        return cls(
            server=server,
            jwks=jwt.PyJWKClient(server.jwks_uri, lifespan=JWKS_CACHE_SECONDS),
        )

    def is_from_issuer(self, token: str) -> bool:
        """Whether an unverified token names this issuer; routing only, not trust."""
        try:
            claims = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError:
            return False
        return claims.get("iss") == self.server.issuer

    # ast-grep-ignore: no-dict-any - JWT claims are issuer-defined JSON
    async def verify(self, token: str) -> dict[str, Any] | None:
        """The token's claims if it is valid for this endpoint, else None.

        An unreachable key endpoint propagates: it is a deployment fault, not a
        bad credential, and should look like one.
        """
        try:
            signing_key = await self._signing_key(token)
        except jwt.PyJWKClientConnectionError:
            raise
        except jwt.PyJWTError as exc:
            logger.warning("Rejected MCP access token: no signing key (%s).", exc)
            return None
        if signing_key is None:
            logger.warning("Rejected MCP access token: unknown signing key.")
            return None
        try:
            return jwt.decode(
                token,
                signing_key.key,
                algorithms=ALLOWED_ALGORITHMS,
                audience=self.server.audience,
                issuer=self.server.issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            logger.warning("Rejected MCP access token: %s.", exc)
            return None

    async def _signing_key(self, token: str) -> jwt.PyJWK | None:
        """The issuer's key the token names, refetching the set on a miss if due.

        The throttle is decided here on the event loop rather than in the
        worker thread, so concurrent misses cannot all claim the same refresh.
        """
        kid = jwt.get_unverified_header(token).get("kid")
        if not isinstance(kid, str):
            return None
        keys = await asyncio.to_thread(self.jwks.get_signing_keys)
        key = self.jwks.match_kid(keys, kid)
        if key is not None:
            return key
        now = time.monotonic()
        if now - self.last_unknown_key_refresh < UNKNOWN_KEY_REFRESH_SECONDS:
            return None
        self.last_unknown_key_refresh = now
        keys = await asyncio.to_thread(self.jwks.get_signing_keys, True)
        return self.jwks.match_kid(keys, kid)


def verifier_for(
    app_state: State, server: MCPExternalAuthorizationServer
) -> ExternalTokenVerifier:
    """The verifier for ``server``, kept on the app so its key cache survives requests."""
    existing = getattr(app_state, "mcp_external_token_verifier", None)
    if isinstance(existing, ExternalTokenVerifier) and existing.server == server:
        return existing
    verifier = ExternalTokenVerifier.for_server(server)
    app_state.mcp_external_token_verifier = verifier
    return verifier
