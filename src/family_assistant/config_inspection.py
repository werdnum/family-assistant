"""Shared helpers for serializing and redacting application config.

These utilities are used by both the debug API endpoints and the engineer
profile diagnostic tools so they share a single source of truth for how
diagnostic dumps are produced.

**Declared credential fields redact themselves.** A config field holding a
credential is typed :class:`pydantic.SecretStr`, so ``model_dump`` masks it and
nothing here has to recognize it. That is the mechanism to reach for when
adding a field: it is enforced by the type, not by this module guessing from a
name.

What remains here handles the two places a type cannot reach:

* **Credentials embedded inside a larger value** — the password in a database
  DSN, a ``token=`` query parameter in a service URL, an inline PEM block.
  These fields are not wholly secret, and redacting them entirely would destroy
  the host, database and endpoint an operator opens the dump to read.
* **Dynamically-structured config** — ``mcp_config.mcpServers`` entries come
  from operator-written JSON with ``extra="allow"``, so their shape is not
  declared and cannot be annotated. An ``env`` block in particular maps
  operator-chosen variable names to values, which no declared type covers.
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

# Query parameter names that carry credentials. A URL parameter has no declared
# type to consult, so this is matched by name -- but only within a parsed URL,
# never against config field names.
_CREDENTIAL_PARAM_NAMES: frozenset[str] = frozenset({
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "auth_token",
    "client_secret",
    "key",
    "password",
    "passwd",
    "refresh_token",
    "secret",
    "sig",
    "signature",
    "token",
})

_PEM_HEADER_PATTERN = re.compile(r"-----BEGIN [A-Z0-9][A-Z0-9 ]*-----")

# A URL anywhere inside a string value, not only as the whole value: MCP stdio
# arguments embed endpoints as ``--endpoint=https://...``.
_URL_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s\"'<>`]+")
_URL_TRAILING_PUNCTUATION = ".,;:!?)]}>'\""


def _is_credential_parameter_name(name: str) -> bool:
    """Return True for a URL query parameter that carries a credential."""
    normalized = unquote_plus(name).lower().replace("-", "_")
    if normalized in _CREDENTIAL_PARAM_NAMES:
        return True
    # Vendor-namespaced spellings: "X-Amz-Signature" -> "x_amz_signature".
    return any(normalized.endswith(f"_{known}") for known in _CREDENTIAL_PARAM_NAMES)


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

    The rest of the URL -- scheme, user, host, port, path and non-credential
    parameters -- is preserved, since that is what makes a DSN or endpoint
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


def _parses_as_url(value: str) -> bool:
    """Return True if urlsplit accepts the value (it rejects a broken authority)."""
    try:
        urlsplit(value)
    except ValueError:
        return False
    return True


# SQLAlchemy reads a DSN's password as everything between the userinfo's first
# ":" and the "@". That span may hold "/", "?", "#", spaces or percent-encoded
# bytes -- characters urlsplit either stops at or decodes. Matching the raw text
# keeps the replacement on the characters actually present in the value: asking
# SQLAlchemy for the password instead returns it decoded, which then fails to
# match a percent-encoded original.
_DSN_USERINFO_PASSWORD = re.compile(
    r"(?P<prefix>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<user>[^:@/]*):(?P<password>[^@]*)@"
)


def _redact_dsn_password(match: re.Match[str]) -> str:
    """Redact the raw password span of one database URL authority."""
    return f"{match.group('prefix')}{match.group('user')}:{REDACTED}@"


def _redact_database_url_password(value: str) -> str:
    """Redact a password that only the database-URL grammar can locate."""
    return _DSN_USERINFO_PASSWORD.sub(_redact_dsn_password, value)


def redact_sensitive_text(value: str) -> str:
    """Redact credential material embedded inside a string value.

    These shapes are self-identifying: an inline PEM block is a private key
    wherever it appears, and a URL's userinfo password or ``token=`` parameter
    is a credential whether the field is called ``database_url`` or ``url``.
    """
    if _PEM_HEADER_PATTERN.search(value):
        return REDACTED
    if "://" not in value:
        return value
    # The URL pass first: it preserves host and path. The database-grammar pass
    # then catches a password the RFC parser could not see. Doing it the other
    # way round feeds "[REDACTED]" back into urlsplit, which reads the bracket
    # as a broken IPv6 literal and fails the whole value closed.
    value = _URL_PATTERN.sub(_redact_url_match, value)
    return _redact_database_url_password(value)


def _is_environment_mapping(key: object, value: object) -> bool:
    """Return True for an ``env`` block mapping variable names to their values.

    Its keys are environment variable names chosen by whoever wrote the server
    entry, not config fields, so no declared type covers them and their names
    say nothing reliable: ``BRAVE_API_KEY`` looks like a credential and
    ``AUTHORIZATION`` does not, though both hold one. Treat the whole mapping as
    opaque and redact its values; the variable names stay visible, which is what
    shows an operator that the block is wired up.
    """
    return key == "env" and isinstance(value, dict)


def _redact_all_values(obj: Any) -> Any:  # noqa: ANN401 - recursive over arbitrary JSON-like data
    """Redact every scalar reachable from an opaque mapping."""
    if isinstance(obj, str):
        return REDACTED if obj else obj
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float)):
        return REDACTED
    if isinstance(obj, dict):
        return {key: _redact_all_values(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_redact_all_values(item) for item in obj]
    return obj


# ast-grep-ignore: no-dict-any - Recursive config redaction handles arbitrary nested structures
def redact_sensitive_config(obj: Any) -> Any:  # noqa: ANN401 - recursive over arbitrary JSON-like data
    """Redact what a declared ``SecretStr`` cannot: embedded and dynamic values."""
    if isinstance(obj, dict):
        return {
            key: _redact_all_values(value)
            if _is_environment_mapping(key, value)
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
