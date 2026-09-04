"""Shared helpers for serializing and redacting application config.

These utilities are used by both the debug API endpoints and the engineer
profile diagnostic tools so they share a single source of truth for which
fields are sensitive and how profile dumps are produced.

Redaction works on two independent axes, because neither alone is sufficient:

* **Field names** catch values that are opaque on their own (an API key is
  indistinguishable from any other random string).
* **Value shapes** catch credentials embedded inside otherwise-useful values —
  the password in a database DSN, a ``token=`` query parameter in a service
  URL, an inline PEM private key — where redacting the whole field would
  destroy the diagnostic value of the host, database or endpoint around it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote_plus, urlsplit, urlunsplit

if TYPE_CHECKING:
    from family_assistant.config_models import (
        DefaultProfileSettings,
        ServiceProfile,
    )

REDACTED = "[REDACTED]"


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
    "credential_encryption_key",
    "auth_key",
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

# Suffixes that name *where a secret lives* rather than holding one: a
# filesystem path, a filename, an environment variable name, a directory. Their
# values are operator-facing metadata and redacting them hides which knob is
# wired up without protecting anything. A secret misfiled under such a name is
# still caught by the value-shape checks below.
_INDIRECTION_SUFFIXES: tuple[str, ...] = (
    "_path",
    "_paths",
    "_file",
    "_dir",
    "_env",
    "_env_var",
)

# Credential parameter names that the field-name axis does not already catch:
# they carry no secret-looking substring of their own.
_BARE_CREDENTIAL_PARAM_NAMES: frozenset[str] = frozenset({
    "auth",
    "key",
    "sig",
    "signature",
})

_PEM_HEADER_PATTERN = re.compile(r"-----BEGIN [A-Z0-9][A-Z0-9 ]*-----")

# A URL anywhere inside a string value, not only as the whole value: MCP stdio
# arguments and command lines embed endpoints as ``--endpoint=https://...``.
_URL_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s\"'<>`]+")
_URL_TRAILING_PUNCTUATION = ".,;:!?)]}>'\""


def is_sensitive_field_name(key: object) -> bool:
    """Return True if the given dict key looks like it carries a secret."""
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    if lowered.endswith(_INDIRECTION_SUFFIXES):
        return False
    if key in SENSITIVE_FIELD_NAMES:
        return True
    return any(substring in lowered for substring in _SENSITIVE_SUBSTRINGS)


def _is_credential_parameter_name(name: str) -> bool:
    """Return True for a URL query parameter or command-line option holding a secret.

    Reuses the field-name axis rather than keeping a second list beside it, so a
    name it already knows (``client_secret``, ``access_token``, ``*_api_key``)
    is recognized here too, and its indirection suffixes still apply.
    """
    normalized = unquote_plus(name).lower().replace("-", "_")
    if normalized in _BARE_CREDENTIAL_PARAM_NAMES or is_sensitive_field_name(
        normalized
    ):
        return True
    # Vendor-namespaced spellings of the same names: "X-Amz-Signature" and
    # "X-Goog-Signature" both normalize to something ending in "_signature".
    return any(normalized.endswith(f"_{name}") for name in _BARE_CREDENTIAL_PARAM_NAMES)


def _redact_query_pair(pair: str) -> str:
    """Redact one raw ``name=value`` query pair, leaving its encoding intact."""
    name, separator, raw_value = pair.partition("=")
    if not separator or not raw_value:
        return pair
    if not _is_credential_parameter_name(name):
        return pair
    return f"{name}={REDACTED}"


def _redact_url(value: str) -> str:
    """Strip userinfo passwords and credential query parameters from a URL.

    The rest of the URL — scheme, user, host, port, path and non-credential
    parameters — is preserved, since that is what makes a DSN or endpoint
    useful in a diagnostic dump in the first place.
    """
    try:
        split = urlsplit(value)
    except ValueError:
        # Fail closed. A malformed endpoint is exactly what an operator reaches
        # for the diagnostics to look at, and it may still carry a password we
        # cannot locate without parsing it.
        return REDACTED

    netloc = split.netloc
    if "@" in netloc:
        userinfo, _, hostinfo = netloc.rpartition("@")
        if ":" in userinfo:
            user, _, _password = userinfo.partition(":")
            netloc = f"{user}:{REDACTED}@{hostinfo}"

    # Rewrite the raw query rather than parse_qsl output: decoding and
    # re-joining would turn an escaped separator (``filter=a%26b``) into two
    # parameters and expose percent-encoded nested URLs in decoded form.
    query = split.query
    if query:
        query = "&".join(_redact_query_pair(pair) for pair in query.split("&"))

    if netloc == split.netloc and query == split.query:
        return value
    return urlunsplit((split.scheme, netloc, split.path, query, split.fragment))


def _parses_as_url(value: str) -> bool:
    """Return True if urlsplit accepts the value (it rejects a broken authority)."""
    try:
        urlsplit(value)
    except ValueError:
        return False
    return True


def _redact_url_match(match: re.Match[str]) -> str:
    """Redact one URL found inside a larger string, keeping trailing punctuation."""
    url = match.group(0)
    stripped = url.rstrip(_URL_TRAILING_PUNCTUATION)
    trailing = url[len(stripped) :]
    # A trailing character is only prose punctuation if the URL still parses
    # without it. It may instead close an IPv6 literal host
    # ("https://user:pw@[::1]"), where stripping it would break parsing and
    # leave the credential in place.
    if trailing and stripped and _parses_as_url(stripped):
        return _redact_url(stripped) + trailing
    return _redact_url(url)


def redact_sensitive_text(value: str) -> str:
    """Redact credential material embedded inside a string value.

    Applies regardless of the field name the string was found under, because
    these shapes are self-identifying: an inline PEM block is a private key
    wherever it appears, and a URL's userinfo password or ``token=`` parameter
    is a credential whether the field is called ``database_url`` or ``url``.
    """
    if _PEM_HEADER_PATTERN.search(value):
        return REDACTED
    if "://" not in value:
        return value
    return _URL_PATTERN.sub(_redact_url_match, value)


def _is_environment_mapping(key: object, value: object) -> bool:
    """Return True for an ``env`` block mapping variable names to their values.

    Its keys are environment variable names chosen by whoever wrote the server
    entry, not config field names, so the name axis cannot judge them:
    ``BRAVE_API_KEY`` happens to match and ``AUTHORIZATION`` does not, though
    both hold a credential. Treat the whole mapping as opaque and redact its
    values; the variable names stay visible, which is what an operator needs to
    see that the block is wired up.
    """
    return key == "env" and isinstance(value, dict)


# ast-grep-ignore: no-dict-any - Recursive config redaction handles arbitrary nested structures
def _redact_secret_container(obj: Any) -> Any:  # noqa: ANN401 - recursive over arbitrary JSON-like data
    """Redact every string reachable from a value held under a sensitive name.

    Numbers and booleans are passed through: config fields are validated
    against their declared types, so a credential is always a string (or a
    container of strings). This is what keeps ``max_tokens: 4096`` — which
    matches the ``token`` substring — readable.
    """
    if isinstance(obj, str):
        return REDACTED if obj else obj
    if isinstance(obj, dict):
        return {key: _redact_secret_container(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_redact_secret_container(item) for item in obj]
    return obj


# ast-grep-ignore: no-dict-any - Recursive config redaction handles arbitrary nested structures
def redact_sensitive_config(obj: Any) -> Any:  # noqa: ANN401 - recursive over arbitrary JSON-like data
    """Recursively redact sensitive fields in a serialized config structure."""
    if isinstance(obj, dict):
        return {
            key: _redact_secret_container(value)
            if is_sensitive_field_name(key) or _is_environment_mapping(key, value)
            else redact_sensitive_config(value)
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [redact_sensitive_config(item) for item in obj]
    if isinstance(obj, str):
        return redact_sensitive_text(obj)
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
