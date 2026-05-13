"""Shared helpers for serializing and redacting application config.

These utilities are used by both the debug API endpoints and the engineer
profile diagnostic tools so they share a single source of truth for which
fields are sensitive and how profile dumps are produced.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from family_assistant.config_models import (
        DefaultProfileSettings,
        ServiceProfile,
    )


# Fields whose values may leak secrets or per-user credentials. When a key with
# one of these names appears anywhere in the serialized config (including nested
# dicts under e.g. ``camera_config`` or ``home_assistant_*``), its value is
# replaced with "[REDACTED]" in the response.
SENSITIVE_FIELD_NAMES: frozenset[str] = frozenset({
    "home_assistant_token",
    "password",
    "client_secret",
    "mailgun_webhook_signing_key",
    "gemini_api_key",
    "openai_api_key",
    "openrouter_api_key",
    "telegram_token",
    "vapid_private_key",
    "session_secret_key",
    "token_env",
})

# Substring patterns used as a defense-in-depth fallback so config fields added
# in the future with secret-looking names (e.g. ``*_api_key``, ``*_secret``,
# ``*_token``, ``*_password``) are redacted even if not explicitly allowlisted.
_SENSITIVE_SUBSTRINGS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "passwd",
    "private_key",
)


def is_sensitive_field_name(key: object) -> bool:
    """Return True if the given dict key looks like it carries a secret."""
    if not isinstance(key, str):
        return False
    if key in SENSITIVE_FIELD_NAMES:
        return True
    lowered = key.lower()
    return any(substring in lowered for substring in _SENSITIVE_SUBSTRINGS)


# ast-grep-ignore: no-dict-any - Recursive config redaction handles arbitrary nested structures
def redact_sensitive_config(obj: Any) -> Any:  # noqa: ANN401 - recursive over arbitrary JSON-like data
    """Recursively redact sensitive fields in a serialized config structure."""
    if isinstance(obj, dict):
        return {
            key: "[REDACTED]"
            if is_sensitive_field_name(key) and value
            else redact_sensitive_config(value)
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [redact_sensitive_config(item) for item in obj]
    return obj


def dump_profile_like(
    profile: ServiceProfile | DefaultProfileSettings,
    # ast-grep-ignore: no-dict-any - Serialized Pydantic model has heterogeneous top-level values
) -> dict[str, Any]:
    """Serialize a ServiceProfile/DefaultProfileSettings including excluded operator fields.

    ``operator_tools_policy`` on both ``ServiceProfile`` and
    ``DefaultProfileSettings`` is declared with ``exclude=True`` so it does not
    round-trip through the YAML config, but it is merged with the
    profile/defaults layers at runtime (see ``PolicyEngine.from_layers``). For
    diagnostic dumps we serialize it explicitly so callers can see every layer
    contributing to the effective policy.
    """
    dumped = profile.model_dump(mode="json")

    if profile.operator_tools_policy is not None:
        dumped["operator_tools_policy"] = profile.operator_tools_policy.model_dump(
            mode="json"
        )
    else:
        dumped["operator_tools_policy"] = None

    return dumped
