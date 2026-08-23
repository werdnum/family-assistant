"""Single source of truth for `/api` route authentication classification.

Every `/api/*` request is authenticated by default (session or API token) in
:class:`~family_assistant.web.auth.AuthMiddleware`. The paths below are the
complete set of exceptions, each authenticated by something else or public by
design:

- ``bootstrap``: endpoints whose purpose is obtaining a credential; they carry
  their own authentication (PKCE code + verifier, opaque token, session).
- ``public``: deliberately unauthenticated receivers with their own abuse
  controls.
- ``scoped``: routes whose access control lives outside default auth — either
  a route dependency granting narrower access than default auth (diagnostics
  readonly token) or a custom transport-level credential (the Asterisk
  WebSocket's query-token handshake, which cannot carry headers/cookies).

The same classification drives the edge deployment's JWT-enforcement route
split (see docs/design/jwt-edge-auth.md): it is published at
``/.well-known/auth-route-classification`` so the gateway configuration can be
generated or contract-tested against it rather than hand-mirrored.
"""

from typing import Literal, TypedDict

RouteClass = Literal["bootstrap", "public", "scoped"]

# Functional TypedDict syntax: "class" is a reserved word in class syntax but
# is the published JSON contract's key.
RouteExemption = TypedDict(
    "RouteExemption",
    {"match": str, "path": str, "methods": list[str], "class": str},
)


class RouteClassificationDocument(TypedDict):
    """The document served at /.well-known/auth-route-classification."""

    jwt_required_prefix: str
    no_jwt_routes: list["RouteExemption"]


# (methods, match, path, class). "exact" matches only that path; "prefix"
# matches the path itself and anything beneath it.
NO_DEFAULT_AUTH_ROUTES: list[tuple[frozenset[str], str, str, RouteClass]] = [
    (frozenset({"POST"}), "exact", "/api/auth/exchange", "bootstrap"),
    (frozenset({"POST"}), "exact", "/api/auth/refresh", "bootstrap"),
    (frozenset({"POST"}), "exact", "/api/auth/token", "bootstrap"),
    (frozenset({"GET"}), "exact", "/api/auth/browser-token", "bootstrap"),
    # OAuth return target: the browser can come back from the provider's
    # consent page after arbitrarily long (the JWT cookie may have expired
    # while away), so the gateway must let it through to the backend's own
    # state + session validation.
    (
        frozenset({"GET"}),
        "exact",
        "/api/integrations/google/callback",
        "bootstrap",
    ),
    # Custom-auth transports: the Asterisk WebSocket authenticates with its
    # own ?token= secret during the handshake and cannot present a JWT or
    # browser cookie. Any future non-HTTP-auth transport joins this class.
    (frozenset({"GET"}), "exact", "/api/asterisk/live", "scoped"),
    (frozenset({"POST"}), "exact", "/api/errors/", "public"),
    # Scoped: diagnostics readonly token checked in get_diagnostics_reader.
    # Narrow to exactly the routes that accept it; sibling debug routes use
    # default authentication.
    (frozenset({"GET"}), "exact", "/api/debug/profiles/tools", "scoped"),
    (frozenset({"GET"}), "prefix", "/api/diagnostics", "scoped"),
    (frozenset({"GET"}), "prefix", "/api/errors/", "scoped"),
]


def is_api_path(path: str) -> bool:
    """Whether a request path is under the API prefix."""
    return path == "/api" or path.startswith("/api/")


def api_route_requires_default_auth(method: str, path: str) -> bool:
    """Whether an /api request must pass default middleware authentication.

    True for everything under /api that is not in NO_DEFAULT_AUTH_ROUTES —
    fail-closed: an unmatched or misclassified path still requires auth.
    """
    if not is_api_path(path):
        return False
    for methods, match, route_path, _ in NO_DEFAULT_AUTH_ROUTES:
        if method.upper() not in methods:
            continue
        if match == "exact":
            if path == route_path:
                return False
        else:
            prefix = route_path.rstrip("/")
            if path == prefix or path.startswith(f"{prefix}/"):
                return False
    return True


def api_route_classification() -> RouteClassificationDocument:
    """The published classification document for edge policy generation."""
    return {
        "jwt_required_prefix": "/api/",
        "no_jwt_routes": [
            {
                "match": match,
                "path": route_path,
                "methods": sorted(methods),
                "class": route_class,
            }
            for methods, match, route_path, route_class in NO_DEFAULT_AUTH_ROUTES
        ],
    }
