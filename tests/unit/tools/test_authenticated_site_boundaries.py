"""Regressions for the ways an authenticated run could escape its session.

Each test here corresponds to a defect found in review. They are grouped
because they share one property: in every case the failure was silent -- the
run kept going with a wider boundary, or with no boundary at all -- which is
exactly the shape of failure the design says must not be possible.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import httpx
import pytest
from pydantic import ValidationError

from family_assistant.config_loader import load_config
from family_assistant.config_models import (
    AppConfig,
    BrowserHandoffConfig,
    RemoteA2AAuthConfig,
)
from family_assistant.tools.authenticated_sites import lease_not_reclaimable
from family_assistant.tools.browser_backend import (
    AuthenticatedSessionBinding,
    AuthenticatedSessionSpec,
    AuthenticatedSessionUnavailableError,
    RemoteBrowserBackend,
    authenticated_binding_for,
    bind_authenticated_session,
    get_browser_backend,
    release_authenticated_session,
    release_borrowed_session,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from family_assistant.storage.delegation_runs import AuthenticatedSiteEnvelope
    from family_assistant.tools.types import ToolExecutionContext

ORIGIN = "https://www.hellofresh.com.au"

SITE = {
    "display_name": "HelloFresh",
    "jar_id": "jar_1",
    "start_url": f"{ORIGIN}/menus",
    "authenticated_origins": [ORIGIN],
    "credential_alias": "hellofresh",
    "authorized_users": ["andrew"],
    "caller_profiles": ["default_assistant"],
    "damage_envelope": "Meals only.",
}


@pytest.fixture(autouse=True)
def _service_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_HANDOFF_SERVICE_TOKEN", "test-token")


@pytest.fixture(scope="module")
def configured() -> AppConfig:
    """The shipped configuration with one authenticated site enabled."""
    data = load_config("defaults.yaml").model_dump()
    data["authenticated_sites"] = {"hellofresh": SITE}
    data["browser_handoff_config"] = {
        **data["browser_handoff_config"],
        "enabled": True,
        "service_url": "http://browser-server.test:8000",
        "auth": {"type": "bearer", "token_env": "BROWSER_HANDOFF_SERVICE_TOKEN"},
    }
    return AppConfig.model_validate(data)


def _context(
    config: AppConfig, *, profile_id: str, subconversation_id: str | None
) -> ToolExecutionContext:
    return cast(
        "ToolExecutionContext",
        SimpleNamespace(
            conversation_id="conv-1",
            subconversation_id=subconversation_id,
            processing_profile_id=profile_id,
            processing_service=SimpleNamespace(app_config=config),
            user_name="andrew",
            timezone=None,
            tool_call_batch=None,
            tool_call_id=None,
        ),
    )


def _backend(*, session_state: str = "agent_active") -> RemoteBrowserBackend:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/v1/sessions/" in request.url.path:
            return httpx.Response(
                200, json={"session_id": "bs_auth", "state": session_state}
            )
        return httpx.Response(200, json={"session_id": "bs_auth"})

    return RemoteBrowserBackend(
        BrowserHandoffConfig(
            enabled=True,
            service_url="http://browser-server.test:8000",
            auth=RemoteA2AAuthConfig(
                type="bearer", token_env="BROWSER_HANDOFF_SERVICE_TOKEN"
            ),
        ),
        "conv-1",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0),
        authenticated=AuthenticatedSessionSpec(
            site_id="hellofresh",
            jar_id=None,
            confine_origins=frozenset({ORIGIN}),
            credential_alias="hellofresh",
        ),
    )


@pytest.fixture
def owner_binding() -> Iterator[AuthenticatedSessionBinding]:
    backend = _backend()
    backend.adopt_session("bs_auth")
    binding = AuthenticatedSessionBinding(
        site_id="hellofresh", delegation_id="delegation_1", backend=backend
    )
    bind_authenticated_session("owner-sub", binding)
    try:
        yield binding
    finally:
        release_authenticated_session("owner-sub")


@pytest.mark.asyncio
async def test_an_authenticated_profile_without_a_binding_fails_closed(
    configured: AppConfig,
) -> None:
    """A queued run that outlived its binding must not open a fresh browser.

    This is the state a restart leaves behind: the durable task survives, the
    in-process binding does not. Falling through to the conversation's ordinary
    backend would create an unconfined session for a profile whose whole point
    is that it never gets one.
    """
    context = _context(
        configured,
        profile_id="authenticated_browser_profile",
        subconversation_id="orphaned-sub",
    )
    with pytest.raises(AuthenticatedSessionUnavailableError, match="only operates"):
        await get_browser_backend(context)


@pytest.mark.asyncio
async def test_the_visual_profile_also_fails_closed(
    configured: AppConfig,
) -> None:
    context = _context(
        configured,
        profile_id="authenticated_browser_visual_profile",
        subconversation_id="orphaned-sub",
    )
    with pytest.raises(AuthenticatedSessionUnavailableError):
        await get_browser_backend(context)


@pytest.mark.asyncio
async def test_an_ordinary_profile_still_gets_the_conversation_backend(
    configured: AppConfig,
) -> None:
    """The fail-closed rule is scoped to profiles the configuration names."""
    context = _context(
        configured, profile_id="browser_profile", subconversation_id=None
    )
    backend = await get_browser_backend(context)
    assert isinstance(backend, RemoteBrowserBackend)
    assert backend.authenticated_spec is None


@pytest.mark.asyncio
async def test_a_borrowed_binding_resolves_for_the_child_turn(
    configured: AppConfig, owner_binding: AuthenticatedSessionBinding
) -> None:
    """The visual hop shares the authenticated tab, not a second browser.

    The inline hop mints a subconversation with no delegation-run row, so there
    is no parent link to walk; the binding has to be handed down explicitly.
    """
    bind_authenticated_session("child-sub", owner_binding)
    try:
        context = _context(
            configured,
            profile_id="authenticated_browser_visual_profile",
            subconversation_id="child-sub",
        )
        assert await get_browser_backend(context) is owner_binding.backend
    finally:
        release_borrowed_session("child-sub")


def test_releasing_a_borrowed_binding_leaves_the_owner_bound(
    owner_binding: AuthenticatedSessionBinding,
) -> None:
    """The child's release must not take the parent's session with it.

    Both entries name the same binding object, so the owner's cascading release
    would clear the parent too -- ending the run after its first visual hop.
    """
    bind_authenticated_session("child-sub", owner_binding)
    release_borrowed_session("child-sub")
    assert authenticated_binding_for("child-sub") is None
    assert authenticated_binding_for("owner-sub") is owner_binding


def test_releasing_the_owner_clears_its_borrowers(
    owner_binding: AuthenticatedSessionBinding,
) -> None:
    bind_authenticated_session("child-sub", owner_binding)
    release_authenticated_session("owner-sub")
    assert authenticated_binding_for("child-sub") is None
    assert authenticated_binding_for("owner-sub") is None


def test_a_global_grant_by_glob_is_counted_against_the_profile(
    configured: AppConfig,
) -> None:
    """A glob reaches an authenticated profile exactly as a literal name does.

    Global grants land in a layer a profile's own policy cannot refuse, so a
    grant of `browser_*` has to be excluded like any other -- skipping it would
    make the widest kind of grant the one the check never sees.
    """
    data = configured.model_dump()
    data["global_tools_policy"] = {
        "rules": [
            {
                "match": {"names": ["report_technical_problem", "jq_query", "read_*"]},
                "decision": "allow",
                "priority": 50,
            }
        ]
    }
    # `read_*` pulls in tools the profile never named, and each has to be
    # accounted for: the exclusion list is checked against what the glob
    # actually reaches, not against the literal names beside it.
    with pytest.raises(ValidationError, match="read_source_file"):
        AppConfig.model_validate(data)


def test_a_global_grant_by_tag_rejects_the_site_configuration(
    configured: AppConfig,
) -> None:
    """What a tag matcher hands a profile is not knowable at startup."""
    data = configured.model_dump()
    data["global_tools_policy"] = {
        "rules": [
            {
                "match": {"tags_any": ["read_only"]},
                "decision": "allow",
                "priority": 50,
            }
        ]
    }
    with pytest.raises(ValidationError, match="by tag or MCP server"):
        AppConfig.model_validate(data)


def test_a_global_deny_by_tag_is_not_a_grant(configured: AppConfig) -> None:
    """Only granting rules are unanalysable in the way that matters."""
    data = configured.model_dump()
    data["global_tools_policy"] = {
        "rules": [
            *data["global_tools_policy"]["rules"],
            {
                "match": {"tags_any": ["state_changing"]},
                "decision": "deny",
                "priority": 99,
            },
        ]
    }
    assert AppConfig.model_validate(data).authenticated_sites


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", ["handoff_requested", "human_active", "handover_requested"]
)
async def test_a_parked_run_stays_parked_until_the_agent_holds_the_lease(
    state: str,
) -> None:
    """Resuming before handback would end the run, not continue it.

    The worker's first browser command on a session it does not hold is
    refused, and an authenticated session is never re-provisioned, so starting
    the turn early turns a resumable park into a dead run. `handover_requested`
    counts as not held: the human has finished, but the agent has not been
    given control back yet.
    """
    backend = _backend(session_state=state)
    backend.adopt_session("bs_auth")
    binding = AuthenticatedSessionBinding(
        site_id="hellofresh", delegation_id="delegation_1", backend=backend
    )
    envelope: AuthenticatedSiteEnvelope = {
        "site_id": "hellofresh",
        "status": "handoff_pending",
        "session_id": "bs_auth",
    }
    parked = await lease_not_reclaimable(binding, envelope)
    assert parked is not None
    assert parked.get_data() == {**envelope, "status": "handoff_pending"}


@pytest.mark.asyncio
async def test_a_reclaimed_session_lets_the_run_continue() -> None:
    backend = _backend(session_state="agent_active")
    backend.adopt_session("bs_auth")
    binding = AuthenticatedSessionBinding(
        site_id="hellofresh", delegation_id="delegation_1", backend=backend
    )
    envelope: AuthenticatedSiteEnvelope = {
        "site_id": "hellofresh",
        "status": "handoff_pending",
        "session_id": "bs_auth",
    }
    assert await lease_not_reclaimable(binding, envelope) is None
