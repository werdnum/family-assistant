"""Authenticated-site session creation, verification, and fail-closed commands.

These are the boundaries that have to hold even when page content completely
controls the browser model, so each is tested against a fake browser-server
rather than asserted about in prose.
"""

from __future__ import annotations

import json
from functools import partial
from typing import TYPE_CHECKING

import httpx
import pytest

from family_assistant.config_models import BrowserHandoffConfig, RemoteA2AAuthConfig
from family_assistant.tools.browser_backend import (
    AuthenticatedSessionMismatchError,
    AuthenticatedSessionSpec,
    AuthenticatedSessionUnavailableError,
    RemoteBrowserBackend,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from family_assistant.tools.browser_backend import JsonDict

ORIGIN = "https://www.hellofresh.com.au"


@pytest.fixture(autouse=True)
def _service_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_HANDOFF_SERVICE_TOKEN", "test-token")


def _config() -> BrowserHandoffConfig:
    return BrowserHandoffConfig(
        enabled=True,
        service_url="http://browser-server.test:8000",
        auth=RemoteA2AAuthConfig(
            type="bearer", token_env="BROWSER_HANDOFF_SERVICE_TOKEN"
        ),
    )


def _spec(
    *, jar_id: str | None = "jar_1", alias: str | None = "hellofresh"
) -> AuthenticatedSessionSpec:
    return AuthenticatedSessionSpec(
        site_id="hellofresh",
        jar_id=jar_id,
        confine_origins=frozenset({ORIGIN}),
        credential_alias=alias,
    )


def _server(
    *,
    session_body: JsonDict | None = None,
    command_status: int = 200,
    command_detail: str = "",
    jar_body: JsonDict | None = None,
    autofill_body: JsonDict | None = None,
) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    """A fake browser-server whose session and command responses are steerable."""
    seen: list[httpx.Request] = []
    created: JsonDict = (
        session_body
        if session_body is not None
        else {
            "session_id": "bs_auth",
            "state": "agent_active",
            "authenticated_site": True,
            "confine_origins": [ORIGIN],
            "jar_origins": [ORIGIN],
            "jar_nav_allowlist": [],
            "jar_generation": 3,
            "credential_alias": "hellofresh",
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/v1/sessions":
            return httpx.Response(200, json=created)
        if path.startswith("/v1/jars/jar_missing"):
            return httpx.Response(404, json={"detail": "unknown jar"})
        if path.startswith("/v1/jars/") and path.endswith("/probe"):
            return httpx.Response(200, json={"jar_id": "jar_1", "fresh": True})
        if path.startswith("/v1/jars/"):
            return httpx.Response(
                200,
                json=jar_body
                or {"jar_id": "jar_1", "generation": 3, "invalidated_at": None},
            )
        if path.endswith("/autofill"):
            return httpx.Response(
                200, json=autofill_body or {"status": "filled", "origin": ORIGIN}
            )
        if path.endswith("/autofill/outcome"):
            return httpx.Response(200, json={"autofill_bad_password": True})
        if path.endswith("/agent-command"):
            if command_status != 200:
                return httpx.Response(command_status, json={"detail": command_detail})
            return httpx.Response(
                200,
                json={
                    "command_id": "cmd_1",
                    "ok": True,
                    "result": {"url": f"{ORIGIN}/menus", "title": "Menus"},
                },
            )
        if path.endswith("/close"):
            return httpx.Response(200, json={"state": "cancelled"})
        return httpx.Response(404, json={"detail": "not found"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0), seen


async def _backend(
    make: Callable[[], tuple[httpx.AsyncClient, list[httpx.Request]]],
    spec: AuthenticatedSessionSpec | None = None,
) -> tuple[RemoteBrowserBackend, list[httpx.Request]]:
    client, seen = make()
    backend = RemoteBrowserBackend(
        _config(), "conv_1", client=client, authenticated=spec or _spec()
    )
    return backend, seen


@pytest.mark.asyncio
async def test_authenticated_session_sends_the_configured_pin() -> None:
    backend, seen = await _backend(_server)
    session_id = await backend.start_authenticated_session(expected_jar_generation=3)
    assert session_id == "bs_auth"
    body = json.loads(next(r for r in seen if r.url.path == "/v1/sessions").content)
    assert body["authenticated_site"] is True
    assert body["jar_id"] == "jar_1"
    assert body["confine_origins"] == [ORIGIN]
    assert body["credential_alias"] == "hellofresh"
    # Never widened: the two fields that would undo confinement or reopen page
    # evaluation are not sent at all.
    assert "allow_exec" not in body
    assert "confine_navigation" not in body
    await backend.close()


@pytest.mark.asyncio
async def test_jarless_session_omits_the_jar_but_keeps_confinement() -> None:
    server = partial(
        _server,
        session_body={
            "session_id": "bs_auth",
            "authenticated_site": True,
            "confine_origins": [ORIGIN],
            "credential_alias": "hellofresh",
        },
    )
    backend, seen = await _backend(server, _spec(jar_id=None))
    await backend.start_authenticated_session()
    body = json.loads(next(r for r in seen if r.url.path == "/v1/sessions").content)
    assert "jar_id" not in body
    assert body["confine_origins"] == [ORIGIN]
    await backend.close()


@pytest.mark.asyncio
async def test_wider_confinement_than_configured_is_refused() -> None:
    server = partial(
        _server,
        session_body={
            "session_id": "bs_auth",
            "authenticated_site": True,
            "confine_origins": [ORIGIN, "https://sso.example.com"],
        },
    )
    backend, _ = await _backend(server)
    with pytest.raises(AuthenticatedSessionMismatchError, match="confined the session"):
        await backend.start_authenticated_session()
    # The session is closed rather than left running on a wider boundary.
    assert backend.session_id is None


@pytest.mark.asyncio
async def test_jar_reaching_undeclared_origins_is_refused() -> None:
    server = partial(
        _server,
        session_body={
            "session_id": "bs_auth",
            "authenticated_site": True,
            "confine_origins": [ORIGIN],
            "jar_origins": [ORIGIN],
            "jar_nav_allowlist": ["https://sso.example.com"],
        },
    )
    backend, _ = await _backend(server)
    with pytest.raises(AuthenticatedSessionMismatchError, match="does not declare"):
        await backend.start_authenticated_session()


@pytest.mark.asyncio
async def test_changed_jar_generation_is_refused() -> None:
    backend, _ = await _backend(_server)
    with pytest.raises(AuthenticatedSessionMismatchError, match="changed between"):
        await backend.start_authenticated_session(expected_jar_generation=2)


@pytest.mark.asyncio
async def test_session_not_marked_authenticated_is_refused() -> None:
    server = partial(
        _server, session_body={"session_id": "bs_auth", "confine_origins": [ORIGIN]}
    )
    backend, _ = await _backend(server)
    with pytest.raises(
        AuthenticatedSessionMismatchError, match="authenticated-site session"
    ):
        await backend.start_authenticated_session()


@pytest.mark.asyncio
async def test_alias_mismatch_is_refused() -> None:
    server = partial(
        _server,
        session_body={
            "session_id": "bs_auth",
            "authenticated_site": True,
            "confine_origins": [ORIGIN],
            "credential_alias": "someone_elses_account",
        },
    )
    backend, _ = await _backend(server)
    with pytest.raises(AuthenticatedSessionMismatchError, match="pinned credential"):
        await backend.start_authenticated_session()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "detail"),
    [(410, "gone"), (404, "unknown session"), (403, "the lease is held by a human")],
)
async def test_navigate_is_never_re_provisioned(status: int, detail: str) -> None:
    """The ordinary backend replaces a lost session on navigate; this one never does."""
    server = partial(_server, command_status=status, command_detail=detail)
    backend, seen = await _backend(server)
    await backend.start_authenticated_session(expected_jar_generation=3)
    with pytest.raises(AuthenticatedSessionUnavailableError, match="never replaced"):
        await backend.goto(f"{ORIGIN}/menus")
    assert sum(1 for r in seen if r.url.path == "/v1/sessions") == 1


@pytest.mark.asyncio
async def test_commands_fail_closed_before_a_session_exists() -> None:
    backend, seen = await _backend(_server)
    with pytest.raises(AuthenticatedSessionUnavailableError, match="no longer availab"):
        await backend.goto(f"{ORIGIN}/menus")
    assert not [r for r in seen if r.url.path == "/v1/sessions"]


@pytest.mark.asyncio
async def test_a_second_session_is_never_created_on_one_backend() -> None:
    backend, _ = await _backend(_server)
    await backend.start_authenticated_session(expected_jar_generation=3)
    with pytest.raises(Exception, match="created once and never replaced"):
        await backend.start_authenticated_session(expected_jar_generation=3)
    await backend.close()


@pytest.mark.asyncio
async def test_jar_status_and_probe_round_trip() -> None:
    backend, _ = await _backend(_server)
    assert (await backend.get_jar("jar_1"))["generation"] == 3
    assert (await backend.probe_jar("jar_1"))["fresh"] is True
    # A jar the service has forgotten reads as missing rather than raising, so
    # the caller routes on it instead of failing the run.
    assert (await backend.get_jar("jar_missing"))["missing"] is True
    assert (await backend.probe_jar("jar_missing"))["fresh"] is False


@pytest.mark.asyncio
async def test_autofill_carries_the_step_key_and_never_a_secret() -> None:
    backend, seen = await _backend(_server)
    await backend.start_authenticated_session(expected_jar_generation=3)
    response = await backend.autofill(
        step_key="password-2",
        fields=[{"ref": "e12", "kind": "password"}],
        wait_seconds=25,
        context={"site": "hellofresh", "acting_user": "andrew"},
    )
    assert response["status"] == "filled"
    request = next(r for r in seen if r.url.path.endswith("/autofill"))
    body = json.loads(request.content)
    assert body["step_key"] == "password-2"
    assert body["fields"] == [{"ref": "e12", "kind": "password"}]
    # Nothing credential-shaped is sent: the alias is pinned on the session and
    # the value is browser-server's business.
    assert "alias" not in body
    assert "password" not in json.dumps(body["context"])
    await backend.close()
