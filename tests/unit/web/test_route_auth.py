"""Tests for /api route authentication classification (fail-closed defaults)."""

import pytest

from family_assistant.web.route_auth import (
    NO_DEFAULT_AUTH_ROUTES,
    api_route_classification,
    api_route_requires_default_auth,
    is_api_path,
)


@pytest.mark.parametrize(
    ("method", "path", "exempt"),
    [
        # Bootstrap endpoints.
        ("POST", "/api/auth/exchange", True),
        ("POST", "/api/auth/refresh", True),
        ("POST", "/api/auth/token", True),
        ("GET", "/api/auth/browser-token", True),
        # OAuth return target: may arrive after the JWT cookie expired.
        ("GET", "/api/integrations/google/callback", True),
        # Wrong method on a bootstrap endpoint falls through to default auth.
        ("GET", "/api/auth/exchange", False),
        ("POST", "/api/auth/browser-token", False),
        # Public error intake; reads under /api/errors stay scoped-exempt for
        # GET only.
        ("POST", "/api/errors/", True),
        ("GET", "/api/errors/telemetry", True),
        ("GET", "/api/errors/", True),
        ("POST", "/api/errors/telemetry", False),
        # Scoped diagnostics/debug prefixes (GET only).
        ("GET", "/api/diagnostics/export", True),
        ("GET", "/api/debug/profiles/tools", True),
        ("POST", "/api/diagnostics/export", False),
        # Everything else under /api requires default auth — fail closed.
        ("GET", "/api/notes/", False),
        ("POST", "/api/notes/", False),
        ("DELETE", "/api/notes/some-title", False),
        ("GET", "/api/auth/me", False),
        ("GET", "/api/v1/chat/conversations", False),
        # Near-miss paths must not be exempt.
        ("GET", "/api/errorsXtelemetry", False),
        ("GET", "/api/diagnosticsXexport", False),
        ("POST", "/api/auth/exchangeX", False),
    ],
)
def test_classification(method: str, path: str, exempt: bool) -> None:
    assert is_api_path(path)
    assert api_route_requires_default_auth(method, path) is (not exempt)


@pytest.mark.parametrize("path", ["/notes", "/", "/apifake"])
def test_non_api_paths_are_out_of_scope(path: str) -> None:
    """Non-API paths never reach this classifier (PUBLIC_PATHS/middleware own them)."""
    assert not is_api_path(path)


def test_every_declared_route_is_matched_as_declared() -> None:
    """Each declared exemption must actually exempt its own path."""
    for methods, match, route_path, _ in NO_DEFAULT_AUTH_ROUTES:
        method = sorted(methods)[0]
        assert api_route_requires_default_auth(method, route_path) is False
        if match == "prefix":
            assert (
                api_route_requires_default_auth(method, f"{route_path}/deep") is False
            )


def test_is_api_path() -> None:
    assert is_api_path("/api")
    assert is_api_path("/api/")
    assert is_api_path("/api/anything/deeper")
    assert not is_api_path("/")
    assert not is_api_path("/apifake")
    assert not is_api_path("/notes")


def test_published_document_matches_declaration() -> None:
    document = api_route_classification()
    assert document["jwt_required_prefix"] == "/api/"
    published = {
        (entry["match"], entry["path"], tuple(entry["methods"]), entry["class"])
        for entry in document["no_jwt_routes"]
    }
    declared = {
        (match, path, tuple(sorted(methods)), route_class)
        for methods, match, path, route_class in NO_DEFAULT_AUTH_ROUTES
    }
    assert published == declared
