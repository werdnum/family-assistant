"""The autofill tools and the jar routing the high-level tool runs on.

The autofill tools are exercised against a fake browser-server standing in for
the Keychute-backed endpoint, because what matters on this side is that the
run-bound session is the one used, that each login step gets its own reusable
idempotency key, and that nothing credential-shaped crosses back.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import httpx
import pytest

from family_assistant.config_models import (
    AuthenticatedSiteConfig as SiteConfig,
)
from family_assistant.config_models import (
    BrowserHandoffConfig,
    RemoteA2AAuthConfig,
)
from family_assistant.tools.authenticated_sites import route_jar
from family_assistant.tools.browser_autofill import (
    AutofillUnavailableError,
    browser_autofill_tool,
    browser_report_login_outcome_tool,
)
from family_assistant.tools.browser_backend import (
    AuthenticatedSessionBinding,
    AuthenticatedSessionSpec,
    RemoteBrowserBackend,
    bind_authenticated_session,
    release_authenticated_session,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from family_assistant.config_models import AuthenticatedSiteConfig
    from family_assistant.tools.browser_backend import JsonDict
    from family_assistant.tools.types import ToolExecutionContext

ORIGIN = "https://www.hellofresh.com.au"
SUBCONVERSATION = "sub-1"


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


def _exec_context() -> ToolExecutionContext:
    """A context carrying only what these tools read.

    The browser tools reach the session through the run binding, so a namespace
    with the run's subconversation is the whole surface under test here.
    """
    return cast(
        "ToolExecutionContext",
        SimpleNamespace(
            conversation_id="conv-1",
            subconversation_id=SUBCONVERSATION,
            user_name="andrew",
            tool_call_batch=None,
            tool_call_id=None,
            timezone=None,
        ),
    )


def _autofill_server(
    responses: list[JsonDict],
) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    """A browser-server whose autofill endpoint answers from *responses* in turn."""
    seen: list[httpx.Request] = []
    remaining = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/autofill"):
            if remaining:
                body = remaining.pop(0)
            else:
                # The contract's `filled` names the kinds actually written, so
                # the fake echoes back what was asked for.
                body = {
                    "status": "filled",
                    "filled": json.loads(request.content).get("fields") or [],
                }
            return httpx.Response(200, json={**body, "origin": ORIGIN})
        if path.endswith("/autofill/outcome"):
            return httpx.Response(200, json={"autofill_bad_password": True})
        return httpx.Response(404, json={"detail": "not found"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0), seen


@pytest.fixture
def bound(
    request: pytest.FixtureRequest,
) -> Iterator[tuple[AuthenticatedSessionBinding, list[httpx.Request]]]:
    """A run-bound authenticated session over a steerable fake browser-server."""
    responses = cast("list[JsonDict]", getattr(request, "param", []))
    client, seen = _autofill_server(responses)
    backend = RemoteBrowserBackend(
        _config(),
        "conv-1",
        client=client,
        authenticated=AuthenticatedSessionSpec(
            site_id="hellofresh",
            jar_id=None,
            confine_origins=frozenset({ORIGIN}),
            credential_alias="hellofresh",
        ),
    )
    backend.adopt_session("bs_auth")
    binding = AuthenticatedSessionBinding(
        site_id="hellofresh", delegation_id="delegation_1", backend=backend
    )
    bind_authenticated_session(SUBCONVERSATION, binding)
    try:
        yield binding, seen
    finally:
        release_authenticated_session(SUBCONVERSATION)


@pytest.mark.asyncio
async def test_autofill_without_a_bound_session_is_refused() -> None:
    """Autofill exists only on a session whose alias was pinned at creation."""
    with pytest.raises(AutofillUnavailableError, match="not an authenticated-site"):
        await browser_autofill_tool(_exec_context(), kind="password")


@pytest.mark.asyncio
async def test_filled_result_names_the_kind_and_carries_no_value(
    bound: tuple[AuthenticatedSessionBinding, list[httpx.Request]],
) -> None:
    binding, seen = bound
    result = await browser_autofill_tool(
        _exec_context(), field_refs=["e12"], kind="password"
    )
    assert "Filled the stored password" in result.get_text()
    body = json.loads(seen[-1].content)
    assert body["fields"] == [{"ref": "e12", "kind": "password"}]
    assert body["context"] == {"site": "hellofresh", "acting_user": "andrew"}
    assert binding.approval_pending_request_id is None


@pytest.mark.asyncio
async def test_each_login_step_gets_its_own_reusable_key(
    bound: tuple[AuthenticatedSessionBinding, list[httpx.Request]],
) -> None:
    """A retry replays its step's request; the next step opens a new one."""
    binding, seen = bound
    context = _exec_context()
    await browser_autofill_tool(context, kind="username")
    await browser_autofill_tool(context, kind="username")
    await browser_autofill_tool(context, kind="password")
    keys = [json.loads(request.content)["step_key"] for request in seen]
    assert keys == ["username-1", "username-1", "password-2"]
    assert len(binding.step_keys) == 2


@pytest.mark.parametrize(
    "bound",
    [[{"status": "approval_pending", "request_id": "req_7", "detail": "Approve at …"}]],
    indirect=True,
)
@pytest.mark.asyncio
async def test_approval_pending_latches_on_the_binding(
    bound: tuple[AuthenticatedSessionBinding, list[httpx.Request]],
) -> None:
    """The run's outcome is derived from this latch, not from the model's words."""
    binding, _ = bound
    result = await browser_autofill_tool(_exec_context(), kind="password")
    assert "approve" in result.get_text().lower()
    assert binding.approval_pending_request_id == "req_7"


@pytest.mark.parametrize(
    "bound",
    [[{"status": "refused", "reason": "wrong_origin", "detail": "not permitted"}]],
    indirect=True,
)
@pytest.mark.asyncio
async def test_refusal_tells_the_model_not_to_work_around_it(
    bound: tuple[AuthenticatedSessionBinding, list[httpx.Request]],
) -> None:
    _binding, _seen = bound
    result = await browser_autofill_tool(_exec_context(), kind="password")
    text = result.get_text()
    assert "wrong_origin" in text
    assert "Do not enter anything by hand" in text


@pytest.mark.parametrize(
    "bound",
    [[{"status": "refused", "reason": "bad_password_recorded"}]],
    indirect=True,
)
@pytest.mark.asyncio
async def test_a_recorded_bad_password_latches_from_a_refusal_too(
    bound: tuple[AuthenticatedSessionBinding, list[httpx.Request]],
) -> None:
    binding, _ = bound
    await browser_autofill_tool(_exec_context(), kind="password")
    assert binding.bad_password_recorded is True


@pytest.mark.asyncio
async def test_reporting_a_bad_password_latches_and_stops(
    bound: tuple[AuthenticatedSessionBinding, list[httpx.Request]],
) -> None:
    binding, seen = bound
    result = await browser_report_login_outcome_tool(
        _exec_context(), outcome="bad_password"
    )
    assert binding.bad_password_recorded is True
    assert "correct the stored password" in result.get_text()
    assert seen[-1].url.path.endswith("/autofill/outcome")


@pytest.mark.asyncio
async def test_only_a_bad_password_is_reportable(
    bound: tuple[AuthenticatedSessionBinding, list[httpx.Request]],
) -> None:
    _binding, _seen = bound
    with pytest.raises(ValueError, match="must be 'bad_password'"):
        await browser_report_login_outcome_tool(_exec_context(), outcome="success")


@pytest.mark.asyncio
async def test_an_unknown_kind_is_rejected_before_any_request(
    bound: tuple[AuthenticatedSessionBinding, list[httpx.Request]],
) -> None:
    _binding, seen = bound
    with pytest.raises(ValueError, match="must be 'username' or 'password'"):
        await browser_autofill_tool(_exec_context(), kind="totp")
    assert not seen


# --- Jar routing -------------------------------------------------------------


def _site(**changes: object) -> AuthenticatedSiteConfig:
    return SiteConfig.model_validate({
        "display_name": "HelloFresh",
        "jar_id": "jar_1",
        "start_url": f"{ORIGIN}/menus",
        "authenticated_origins": [ORIGIN],
        "credential_alias": "hellofresh",
        "authorized_users": ["andrew"],
        "caller_profiles": ["default_assistant"],
        "damage_envelope": "Meals only.",
        **changes,
    })


def _jar_backend(jar: JsonDict, probe: JsonDict) -> RemoteBrowserBackend:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/probe"):
            return httpx.Response(200, json=probe)
        if jar.get("__missing__"):
            return httpx.Response(404, json={"detail": "unknown jar"})
        return httpx.Response(200, json=jar)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    return RemoteBrowserBackend(
        _config(),
        "conv-1",
        client=client,
        authenticated=AuthenticatedSessionSpec(
            site_id="hellofresh",
            jar_id=None,
            confine_origins=frozenset({ORIGIN}),
            credential_alias="hellofresh",
        ),
    )


@pytest.mark.asyncio
async def test_a_fresh_jar_is_loaded_with_its_generation() -> None:
    backend = _jar_backend({"generation": 4, "invalidated_at": None}, {"fresh": True})
    routing = await route_jar(backend, _site())
    assert routing.jar_id == "jar_1"
    assert routing.generation == 4
    assert routing.login_required is None


@pytest.mark.asyncio
async def test_a_lapsed_jar_with_a_credential_runs_jarless() -> None:
    """Stale state is not worth carrying: the run starts at the login form."""
    backend = _jar_backend({"generation": 4, "invalidated_at": None}, {"fresh": False})
    routing = await route_jar(backend, _site())
    assert routing.jar_id is None
    assert routing.login_required is None


@pytest.mark.asyncio
async def test_a_lapsed_jar_without_a_credential_needs_a_human() -> None:
    backend = _jar_backend({"generation": 4, "invalidated_at": None}, {"fresh": False})
    routing = await route_jar(backend, _site(credential_alias=None))
    assert routing.login_required is not None
    assert "expired" in routing.login_required


@pytest.mark.asyncio
async def test_a_revoked_jar_disables_autofill_as_well() -> None:
    """The kill switch has to mean what it says, alias or no alias."""
    backend = _jar_backend(
        {"generation": 4, "invalidated_at": "2026-09-01T00:00:00Z"}, {"fresh": True}
    )
    routing = await route_jar(backend, _site())
    assert routing.jar_id is None
    assert routing.login_required is not None
    assert "revoked" in routing.login_required


@pytest.mark.asyncio
async def test_a_deleted_jar_is_treated_as_revoked() -> None:
    backend = _jar_backend({"__missing__": True}, {"fresh": True})
    routing = await route_jar(backend, _site())
    assert routing.login_required is not None


@pytest.mark.asyncio
async def test_a_site_with_no_jar_starts_at_the_login_form() -> None:
    backend = _jar_backend({}, {})
    routing = await route_jar(backend, _site(jar_id=None))
    assert routing.jar_id is None
    assert routing.login_required is None


@pytest.mark.parametrize(
    "bound",
    [
        [
            {"status": "approval_pending", "request_id": "req_7"},
            {"status": "refused", "reason": reason},
        ]
        for reason in ("policy_denied", "request_expired", "grant_invalid")
    ],
    indirect=True,
)
@pytest.mark.asyncio
async def test_a_terminal_refusal_clears_the_pending_approval(
    bound: tuple[AuthenticatedSessionBinding, list[httpx.Request]],
) -> None:
    binding, _ = bound
    await browser_autofill_tool(_exec_context(), kind="password")
    assert binding.approval_pending_request_id == "req_7"
    await browser_autofill_tool(_exec_context(), kind="password")
    assert binding.approval_pending_request_id is None
