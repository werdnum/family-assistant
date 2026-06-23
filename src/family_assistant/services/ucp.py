"""Universal Commerce Protocol profile and request signing helpers."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from uuid import uuid4

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

if TYPE_CHECKING:
    from collections.abc import Mapping

    from family_assistant.config_models import AppConfig, UCPConfig


UCP_PROFILE_PATH = "/.well-known/ucp"
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True, slots=True)
class UCPSignedRequest:
    """Prepared UCP request data with HTTP Message Signature headers."""

    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None


class UCPConfigurationError(ValueError):
    """Raised when UCP configuration is incomplete or invalid."""


def _base64_url_no_padding(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _base64_standard(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _without_none_values(data: dict[str, object | None]) -> dict[str, object]:
    return {key: value for key, value in data.items() if value is not None}


def _load_signing_private_key(
    ucp_config: UCPConfig,
) -> ec.EllipticCurvePrivateKey | None:
    private_key_pem = ucp_config.signing_private_key
    if not private_key_pem and ucp_config.signing_private_key_path:
        private_key_pem = Path(ucp_config.signing_private_key_path).read_text(
            encoding="utf-8"
        )
    if not private_key_pem:
        return None

    try:
        private_key = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"), password=None
        )
    except (TypeError, ValueError, UnsupportedAlgorithm) as exc:
        msg = "UCP signing private key must be an unencrypted PEM EC private key."
        raise UCPConfigurationError(msg) from exc

    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
        msg = "UCP signing private key must be an EC private key."
        raise UCPConfigurationError(msg)
    if not isinstance(private_key.curve, (ec.SECP256R1, ec.SECP384R1)):
        msg = "UCP signing private key must use P-256 or P-384."
        raise UCPConfigurationError(msg)
    return private_key


def _jwk_from_private_key(
    private_key: ec.EllipticCurvePrivateKey,
    *,
    key_id: str,
) -> dict[str, object]:
    public_numbers = private_key.public_key().public_numbers()
    if isinstance(private_key.curve, ec.SECP256R1):
        coordinate_size = 32
        curve_name = "P-256"
        algorithm = "ES256"
    elif isinstance(private_key.curve, ec.SECP384R1):
        coordinate_size = 48
        curve_name = "P-384"
        algorithm = "ES384"
    else:
        msg = "UCP signing private key must use P-256 or P-384."
        raise UCPConfigurationError(msg)

    return {
        "kid": key_id,
        "kty": "EC",
        "crv": curve_name,
        "x": _base64_url_no_padding(public_numbers.x.to_bytes(coordinate_size)),
        "y": _base64_url_no_padding(public_numbers.y.to_bytes(coordinate_size)),
        "use": "sig",
        "alg": algorithm,
    }


def _profile_url(app_config: AppConfig) -> str:
    if app_config.ucp_config.profile_url:
        return app_config.ucp_config.profile_url
    return f"{app_config.server_url.rstrip('/')}{app_config.ucp_config.profile_path}"


def ucp_profile_url(app_config: AppConfig) -> str:
    """Return the public UCP profile URL advertised for this application."""
    return _profile_url(app_config)


def ucp_agent_header(app_config: AppConfig) -> str:
    """Return the UCP-Agent header value for this application's profile."""
    return _format_ucp_agent_header(_profile_url(app_config))


def has_ucp_signing_key(app_config: AppConfig) -> bool:
    """Return True when UCP request signing key material is configured."""
    ucp_config = app_config.ucp_config
    return bool(
        ucp_config.signing_key_id
        and (ucp_config.signing_private_key or ucp_config.signing_private_key_path)
    )


def build_ucp_profile(app_config: AppConfig) -> dict[str, object]:
    """Build the public UCP platform profile for this Family Assistant instance."""
    ucp_config = app_config.ucp_config
    if not ucp_config.enabled:
        msg = "UCP profile is disabled."
        raise UCPConfigurationError(msg)

    signing_keys: list[dict[str, object]] = [
        key.model_dump(exclude_none=True) for key in ucp_config.additional_signing_keys
    ]
    private_key = _load_signing_private_key(ucp_config)
    if private_key is not None:
        if not ucp_config.signing_key_id:
            msg = "UCP_SIGNING_KEY_ID is required when UCP signing key material is configured."
            raise UCPConfigurationError(msg)
        signing_keys.insert(
            0, _jwk_from_private_key(private_key, key_id=ucp_config.signing_key_id)
        )

    return {
        "ucp": _without_none_values({
            "version": ucp_config.version,
            "services": {
                name: [
                    entry.model_dump(exclude_none=True, by_alias=True)
                    for entry in entries
                ]
                for name, entries in ucp_config.services.items()
            },
            "capabilities": {
                name: [
                    entry.model_dump(exclude_none=True, by_alias=True)
                    for entry in entries
                ]
                for name, entries in ucp_config.capabilities.items()
            },
            "payment_handlers": {
                name: [
                    entry.model_dump(exclude_none=True, by_alias=True)
                    for entry in entries
                ]
                for name, entries in ucp_config.payment_handlers.items()
            }
            if ucp_config.payment_handlers
            else None,
        }),
        "signing_keys": signing_keys,
    }


def _json_body_bytes(body: object | None) -> bytes | None:
    if body is None:
        return None
    if isinstance(body, str):
        return body.encode("utf-8")
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _content_digest(body: bytes) -> str:
    return f"sha-256=:{_base64_standard(hashlib.sha256(body).digest())}:"


def _format_ucp_agent_header(profile_url: str) -> str:
    if '"' in profile_url or "\\" in profile_url:
        msg = "UCP profile URL must not contain quote or backslash characters."
        raise UCPConfigurationError(msg)
    return f'profile="{profile_url}"'


def _signature_input_header(components: list[str], key_id: str) -> str:
    quoted_components = " ".join(f'"{component}"' for component in components)
    escaped_key_id = key_id.replace("\\", "\\\\").replace('"', '\\"')
    return f'sig1=({quoted_components});keyid="{escaped_key_id}"'


def _component_value(
    component: str,
    *,
    method: str,
    authority: str,
    path: str,
    query: str,
    headers: Mapping[str, str],
) -> str:
    if component == "@method":
        return method
    if component == "@authority":
        return authority
    if component == "@path":
        return path
    if component == "@query":
        return f"?{query}"
    return headers[component]


def _signature_base(
    components: list[str],
    *,
    method: str,
    authority: str,
    path: str,
    query: str,
    headers: Mapping[str, str],
    key_id: str,
) -> bytes:
    lines = [
        f'"{component}": '
        f"{_component_value(component, method=method, authority=authority, path=path, query=query, headers=headers)}"
        for component in components
    ]
    params = _signature_input_header(components, key_id).split("=", 1)[1]
    lines.append(f'"@signature-params": {params}')
    return "\n".join(lines).encode("utf-8")


def _signature_hash(private_key: ec.EllipticCurvePrivateKey) -> hashes.HashAlgorithm:
    if isinstance(private_key.curve, ec.SECP256R1):
        return hashes.SHA256()
    if isinstance(private_key.curve, ec.SECP384R1):
        return hashes.SHA384()
    msg = "UCP signing private key must use P-256 or P-384."
    raise UCPConfigurationError(msg)


def _raw_ecdsa_signature(
    private_key: ec.EllipticCurvePrivateKey,
    signature_base: bytes,
) -> bytes:
    der_signature = private_key.sign(
        signature_base,
        ec.ECDSA(_signature_hash(private_key)),
    )
    r_value, s_value = utils.decode_dss_signature(der_signature)
    coordinate_size = 32 if isinstance(private_key.curve, ec.SECP256R1) else 48
    return r_value.to_bytes(coordinate_size) + s_value.to_bytes(coordinate_size)


def sign_ucp_request(
    app_config: AppConfig,
    *,
    method: str,
    url: str,
    body: object | None = None,
    idempotency_key: str | None = None,
    additional_headers: Mapping[str, str] | None = None,
) -> UCPSignedRequest:
    """Prepare UCP HTTP headers and body bytes for a signed platform request."""
    ucp_config = app_config.ucp_config
    private_key = _load_signing_private_key(ucp_config)
    if private_key is None or not ucp_config.signing_key_id:
        msg = (
            "UCP request signing requires UCP_SIGNING_KEY_ID and either "
            "UCP_SIGNING_PRIVATE_KEY or UCP_SIGNING_PRIVATE_KEY_PATH."
        )
        raise UCPConfigurationError(msg)

    normalized_method = method.upper()
    parsed_url = urlparse(url)
    if parsed_url.scheme != "https":
        msg = "UCP requests must use an https URL."
        raise UCPConfigurationError(msg)
    if not parsed_url.netloc:
        msg = "UCP request URL must include a host."
        raise UCPConfigurationError(msg)

    request_body = _json_body_bytes(body)
    headers = {
        "UCP-Agent": _format_ucp_agent_header(_profile_url(app_config)),
        **(dict(additional_headers or {})),
    }
    if request_body is not None:
        headers["Content-Type"] = headers.get("Content-Type", "application/json")
        headers["Content-Digest"] = _content_digest(request_body)
    if normalized_method in STATE_CHANGING_METHODS:
        headers["Idempotency-Key"] = idempotency_key or str(uuid4())
    elif idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    lowercase_headers = {key.lower(): value for key, value in headers.items()}
    components = ["@method", "@authority", "@path"]
    if parsed_url.query:
        components.append("@query")
    components.append("ucp-agent")
    if "idempotency-key" in lowercase_headers:
        components.append("idempotency-key")
    if request_body is not None:
        components.extend(["content-digest", "content-type"])

    signature_input = _signature_input_header(components, ucp_config.signing_key_id)
    base = _signature_base(
        components,
        method=normalized_method,
        authority=parsed_url.netloc,
        path=parsed_url.path or "/",
        query=parsed_url.query,
        headers=lowercase_headers,
        key_id=ucp_config.signing_key_id,
    )
    signature = _raw_ecdsa_signature(private_key, base)
    headers["Signature-Input"] = signature_input
    headers["Signature"] = f"sig1=:{_base64_standard(signature)}:"

    return UCPSignedRequest(
        method=normalized_method,
        url=url,
        headers=headers,
        body=request_body,
    )
