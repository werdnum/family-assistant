"""Tests for ES256 JWT access-token issuance and verification."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ec import (
    EllipticCurvePrivateKey,
    generate_private_key,
)
from cryptography.hazmat.primitives.asymmetric.rsa import (
    generate_private_key as generate_rsa_key,
)

from family_assistant.web import jwt_tokens
from family_assistant.web.jwt_tokens import coordinate_bytes


@pytest.fixture
def signing_key_pem() -> str:
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("ascii")


@pytest.fixture(autouse=True)
def _enable_jwt_signing(
    signing_key_pem: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    monkeypatch.setenv("JWT_SIGNING_KEY", signing_key_pem)
    jwt_tokens.init_jwt_signing()
    yield
    jwt_tokens.reset_jwt_signing_for_tests()


def test_enabled_after_init() -> None:
    assert jwt_tokens.jwt_auth_enabled()


def test_disabled_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JWT_SIGNING_KEY", raising=False)
    assert not jwt_tokens.init_jwt_signing()
    assert not jwt_tokens.jwt_auth_enabled()


def test_invalid_pem_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SIGNING_KEY", "not a key")
    with pytest.raises(jwt_tokens.JWTSigningKeyError):
        jwt_tokens.init_jwt_signing()


def test_non_ec_key_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    rsa_key = generate_rsa_key(public_exponent=65537, key_size=2048)
    pem = rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    monkeypatch.setenv("JWT_SIGNING_KEY", pem)
    with pytest.raises(jwt_tokens.JWTSigningKeyError, match="EC private key"):
        jwt_tokens.init_jwt_signing()


def test_mint_and_verify_roundtrip() -> None:
    token = jwt_tokens.mint_access_token("user@example.com", 42)
    assert jwt_tokens.looks_like_jwt(token)
    claims = jwt_tokens.verify_access_token(token)
    assert claims is not None
    assert claims["sub"] == "user@example.com"
    assert claims["tid"] == 42
    assert claims["iss"] == jwt_tokens.JWT_ISSUER
    assert claims["aud"] == jwt_tokens.JWT_AUDIENCE
    assert claims["exp"] - claims["iat"] == pytest.approx(
        jwt_tokens.DEFAULT_ACCESS_TOKEN_TTL_SECONDS, abs=2
    )


def test_tampered_token_rejected() -> None:
    token = jwt_tokens.mint_access_token("user@example.com", 42)
    header, payload, signature = token.split(".")
    tampered = f"{header}.{payload}.AAAA{signature[4:]}"
    assert jwt_tokens.verify_access_token(tampered) is None


def test_wrong_audience_rejected(signing_key_pem: str) -> None:
    private_key = serialization.load_pem_private_key(
        signing_key_pem.encode("ascii"), password=None
    )
    assert isinstance(private_key, EllipticCurvePrivateKey)
    now = datetime.now(UTC)
    token: str = pyjwt.encode(
        {
            "iss": jwt_tokens.JWT_ISSUER,
            "aud": "some-other-audience",
            "sub": "user@example.com",
            "tid": 1,
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        private_key,
        algorithm="ES256",
    )
    assert isinstance(token, str)
    assert jwt_tokens.verify_access_token(token) is None


def test_expired_token_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(jwt_tokens, "access_token_ttl_seconds", lambda: -10)
    token = jwt_tokens.mint_access_token("user@example.com", 42)
    assert jwt_tokens.verify_access_token(token) is None


def test_ttl_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_ACCESS_TOKEN_TTL_SECONDS", "120")
    assert jwt_tokens.access_token_ttl_seconds() == 120
    claims = jwt_tokens.verify_access_token(
        jwt_tokens.mint_access_token("user@example.com", 7)
    )
    assert claims is not None
    assert claims["exp"] - claims["iat"] == pytest.approx(120, abs=2)


def test_invalid_ttl_env_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_ACCESS_TOKEN_TTL_SECONDS", "nope")
    with pytest.raises(jwt_tokens.JWTSigningKeyError):
        jwt_tokens.access_token_ttl_seconds()


def test_jwks_document_shape_and_stable_kid(signing_key_pem: str) -> None:
    jwks = jwt_tokens.jwks_document()
    (key,) = jwks["keys"]
    assert key["kty"] == "EC"
    assert key["crv"] == "P-256"
    assert key["alg"] == "ES256"
    assert key["use"] == "sig"
    assert key["kid"]

    kid_before = key["kid"]
    jwt_tokens.init_jwt_signing()
    assert jwt_tokens.jwks_document()["keys"][0]["kid"] == kid_before


def test_verify_without_signing_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JWT_SIGNING_KEY", raising=False)
    jwt_tokens.reset_jwt_signing_for_tests()
    shaped_token = "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJ1In0.sig"
    assert jwt_tokens.looks_like_jwt(shaped_token)
    assert jwt_tokens.verify_access_token(shaped_token) is None


def test_non_p256_ec_key_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    p384 = generate_private_key(ec.SECP384R1())
    pem = p384.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    monkeypatch.setenv("JWT_SIGNING_KEY", pem)
    with pytest.raises(jwt_tokens.JWTSigningKeyError, match="P-256"):
        jwt_tokens.init_jwt_signing()


def test_coordinate_encoding_is_fixed_width() -> None:
    """A coordinate with a leading zero byte still encodes at 32 bytes."""
    assert len(coordinate_bytes(1)) == 32
    assert coordinate_bytes(0)[0] == 0
    assert coordinate_bytes(2**256 - 1) == b"\xff" * 32


def test_invalid_ttl_fails_at_startup(
    signing_key_pem: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JWT_SIGNING_KEY", signing_key_pem)
    monkeypatch.setenv("JWT_ACCESS_TOKEN_TTL_SECONDS", "nope")
    with pytest.raises(jwt_tokens.JWTSigningKeyError, match="TTL"):
        jwt_tokens.init_jwt_signing()
