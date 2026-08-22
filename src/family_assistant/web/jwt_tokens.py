"""Short-lived ES256 access-token JWTs for API clients.

When ``JWT_SIGNING_KEY`` is configured, token endpoints return signed JWTs
instead of opaque secrets so that a gateway can verify them statelessly (see
docs/design/jwt-edge-auth.md). The database token row remains the revocation
registry: the JWT carries the row id in its ``tid`` claim and every request is
still checked against it.

Unset key ⇒ feature disabled: issuance and verification fall back to today's
opaque-token behaviour.
"""

import base64
import hashlib
import logging
import os
from datetime import UTC, datetime
from typing import TypedDict, cast

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey

JWT_SIGNING_KEY_ENV_VAR = "JWT_SIGNING_KEY"
ACCESS_TOKEN_TTL_ENV_VAR = "JWT_ACCESS_TOKEN_TTL_SECONDS"

DEFAULT_ACCESS_TOKEN_TTL_SECONDS = 3600
JWT_ISSUER = "family-assistant"
JWT_AUDIENCE = "family-assistant-api"

logger = logging.getLogger(__name__)

_signing_key: EllipticCurvePrivateKey | None = None
_key_id: str | None = None


class JWTSigningKeyError(RuntimeError):
    """The configured signing key could not be parsed."""


def init_jwt_signing() -> bool:
    """Load the configured signing key; fail fast on an unparsable value.

    Returns True when JWT auth is enabled. Called once at application startup.
    """
    global _signing_key, _key_id

    pem = os.environ.get(JWT_SIGNING_KEY_ENV_VAR)
    if not pem:
        _signing_key = None
        _key_id = None
        return False

    try:
        loaded_key = serialization.load_pem_private_key(
            pem.encode("utf-8"), password=None
        )
    except Exception as exc:
        raise JWTSigningKeyError(
            f"{JWT_SIGNING_KEY_ENV_VAR} is set but could not be parsed as a "
            f"private key in PEM format: {exc}"
        ) from exc

    if not isinstance(loaded_key, EllipticCurvePrivateKey):
        raise JWTSigningKeyError(
            f"{JWT_SIGNING_KEY_ENV_VAR} must be an EC private key (ES256); "
            f"got {type(loaded_key).__name__}."
        )

    if loaded_key.curve.name != "secp256r1":
        raise JWTSigningKeyError(
            f"{JWT_SIGNING_KEY_ENV_VAR} must be a P-256 (prime256v1) EC key "
            f"for ES256; got curve {loaded_key.curve.name}."
        )

    _signing_key = loaded_key
    _key_id = _compute_key_id(loaded_key)
    logger.info("JWT access-token signing enabled (kid=%s).", _key_id)
    return True


def jwt_auth_enabled() -> bool:
    """Whether signed-JWT issuance/verification is configured."""
    return _signing_key is not None


def reset_jwt_signing_for_tests() -> None:
    """Clear module state between tests."""
    global _signing_key, _key_id
    _signing_key = None
    _key_id = None


def access_token_ttl_seconds() -> int:
    raw = os.environ.get(ACCESS_TOKEN_TTL_ENV_VAR)
    if not raw:
        return DEFAULT_ACCESS_TOKEN_TTL_SECONDS
    try:
        ttl = int(raw)
    except ValueError as exc:
        raise JWTSigningKeyError(
            f"{ACCESS_TOKEN_TTL_ENV_VAR} must be an integer number of seconds."
        ) from exc
    if ttl <= 0:
        raise JWTSigningKeyError(
            f"{ACCESS_TOKEN_TTL_ENV_VAR} must be a positive number of seconds."
        )
    return ttl


class AccessTokenClaims(TypedDict):
    """Claims carried by every issued access token."""

    iss: str
    aud: str
    sub: str
    tid: int
    iat: int
    exp: int
    jti: str


class JsonWebKey(TypedDict):
    """The EC public-key JWK published for gateway verification."""

    kty: str
    crv: str
    x: str
    y: str
    kid: str
    alg: str
    use: str


def mint_access_token(user_identifier: str, api_token_id: int) -> str:
    """Mint a short-lived ES256 access token bound to an api_tokens row."""
    if _signing_key is None or _key_id is None:
        raise RuntimeError("JWT signing is not configured.")
    now = datetime.now(UTC)
    ttl = access_token_ttl_seconds()
    claims: AccessTokenClaims = {
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "sub": user_identifier,
        "tid": api_token_id,
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + ttl,
        "jti": hashlib.sha256(f"{api_token_id}:{now.timestamp()}".encode()).hexdigest(),
    }
    return jwt.encode(
        dict(claims), _signing_key, algorithm="ES256", headers={"kid": _key_id}
    )


def verify_access_token(token: str) -> AccessTokenClaims | None:
    """Verify signature/expiry/issuer/audience; return claims or None.

    Callers must still check the ``tid`` row's revocation status.
    """
    if _signing_key is None:
        return None
    try:
        return cast(
            "AccessTokenClaims",
            jwt.decode(
                token,
                _signing_key.public_key(),
                algorithms=["ES256"],
                issuer=JWT_ISSUER,
                audience=JWT_AUDIENCE,
                options={"require": ["exp", "iss", "aud", "sub", "tid"]},
            ),
        )
    except jwt.PyJWTError as exc:
        logger.debug("JWT verification failed: %s", exc)
        return None


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _int_to_bytes(value: int) -> bytes:
    return value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")


def jwks_document() -> dict[str, list[JsonWebKey]]:
    """Return the JWKS publishing the verification public key."""
    if _signing_key is None or _key_id is None:
        raise RuntimeError("JWT signing is not configured.")
    public_numbers = _signing_key.public_key().public_numbers()
    key: JsonWebKey = {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64u(_int_to_bytes(public_numbers.x)),
        "y": _b64u(_int_to_bytes(public_numbers.y)),
        "kid": _key_id,
        "alg": "ES256",
        "use": "sig",
    }
    return {"keys": [key]}


def looks_like_jwt(token: str) -> bool:
    """Whether a bearer credential has the JWT shape.

    A compact JWS always starts with the base64url encoding of ``{"`` (i.e.
    ``eyJ``), which the uppercase-alphanumeric opaque-token prefix can never
    produce, so this cleanly discriminates the two credential formats.
    """
    return token.startswith("eyJ") and token.count(".") == 2


def _compute_key_id(key: EllipticCurvePrivateKey) -> str:
    """Stable RFC 7638 JWK thumbprint of the public key, used as kid."""
    public_numbers = key.public_key().public_numbers()
    canonical = (
        '{"crv":"P-256","kty":"EC","x":"'
        + _b64u(_int_to_bytes(public_numbers.x))
        + '","y":"'
        + _b64u(_int_to_bytes(public_numbers.y))
        + '"}'
    )
    return _b64u(hashlib.sha256(canonical.encode("utf-8")).digest())
