from __future__ import annotations

import base64
from typing import Any, cast

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from family_assistant.config_models import AppConfig, UCPConfig
from family_assistant.services.ucp import (
    UCPConfigurationError,
    build_ucp_profile,
    sign_ucp_request,
)


def _private_key_pem() -> str:
    private_key = ec.generate_private_key(ec.SECP256R1())
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


def test_build_ucp_profile_publishes_public_jwk_for_signing_key() -> None:
    config = AppConfig(
        server_url="https://assistant.example",
        ucp_config=UCPConfig(
            signing_key_id="platform-2026",
            signing_private_key=_private_key_pem(),
        ),
    )

    profile = cast("dict[str, Any]", build_ucp_profile(config))

    assert profile["ucp"]["version"] == "2026-04-08"
    assert "dev.ucp.shopping" in profile["ucp"]["services"]
    shopping_service = profile["ucp"]["services"]["dev.ucp.shopping"][0]
    assert shopping_service["transport"] == "mcp"
    assert shopping_service["schema"].endswith("/shopping/mcp.openrpc.json")
    assert "dev.ucp.shopping.cart" in profile["ucp"]["capabilities"]
    assert "dev.ucp.shopping.checkout" in profile["ucp"]["capabilities"]
    assert profile["signing_keys"][0]["kid"] == "platform-2026"
    assert profile["signing_keys"][0]["crv"] == "P-256"
    assert profile["signing_keys"][0]["alg"] == "ES256"
    assert "d" not in profile["signing_keys"][0]


def test_sign_ucp_request_adds_required_headers_for_json_post() -> None:
    config = AppConfig(
        server_url="https://assistant.example",
        ucp_config=UCPConfig(
            signing_key_id="platform-2026",
            signing_private_key=_private_key_pem(),
        ),
    )

    request = sign_ucp_request(
        config,
        method="POST",
        url="https://merchant.example/ucp/v1/checkout-sessions?debug=true",
        body={"checkout": {"line_items": [{"id": "sku_123", "quantity": 1}]}},
        idempotency_key="00000000-0000-4000-8000-000000000000",
    )

    assert request.method == "POST"
    assert request.body == (
        b'{"checkout":{"line_items":[{"id":"sku_123","quantity":1}]}}'
    )
    assert request.headers["UCP-Agent"] == (
        'profile="https://assistant.example/.well-known/ucp"'
    )
    assert request.headers["Idempotency-Key"] == (
        "00000000-0000-4000-8000-000000000000"
    )
    assert request.headers["Content-Digest"].startswith("sha-256=:")
    assert '"@query"' in request.headers["Signature-Input"]
    assert '"content-digest"' in request.headers["Signature-Input"]
    signature_value = (
        request.headers["Signature"].removeprefix("sig1=:").removesuffix(":")
    )
    assert len(base64.b64decode(signature_value)) == 64


def test_sign_ucp_get_request_omits_body_headers_and_idempotency_key() -> None:
    config = AppConfig(
        server_url="https://assistant.example",
        ucp_config=UCPConfig(
            signing_key_id="platform-2026",
            signing_private_key=_private_key_pem(),
        ),
    )

    request = sign_ucp_request(
        config,
        method="GET",
        url="https://merchant.example/ucp/v1/products/sku_123",
    )

    assert "Content-Digest" not in request.headers
    assert "Content-Type" not in request.headers
    assert "Idempotency-Key" not in request.headers
    assert request.body is None


def test_sign_ucp_request_rejects_non_https_profile_url() -> None:
    config = AppConfig(
        server_url="http://localhost:8000",
        ucp_config=UCPConfig(
            signing_key_id="platform-2026",
            signing_private_key=_private_key_pem(),
        ),
    )

    with pytest.raises(UCPConfigurationError, match="profile URL must be an HTTPS"):
        sign_ucp_request(
            config,
            method="POST",
            url="https://merchant.example/api/ucp/mcp",
            body={"jsonrpc": "2.0"},
        )
