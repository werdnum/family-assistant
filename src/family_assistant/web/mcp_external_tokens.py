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
from dataclasses import dataclass
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


@dataclass
class ExternalTokenVerifier:
    """Verifies one issuer's tokens against its published signing keys."""

    server: MCPExternalAuthorizationServer
    jwks: jwt.PyJWKClient

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
            signing_key = await asyncio.to_thread(
                self.jwks.get_signing_key_from_jwt, token
            )
        except jwt.PyJWKClientConnectionError:
            raise
        except jwt.PyJWTError as exc:
            logger.warning("Rejected MCP access token: no signing key (%s).", exc)
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
