"""Authenticated-site runs end to end, from a caller turn to a settled session.

A real caller turn calls ``run_authenticated_site_task``; the delegated run it
starts executes under ``authenticated_browser_profile`` on the task worker,
driving a faked browser-server; the worker's outcome is settled back into the
typed result the caller's own turn reads.

The four tests are the four outcomes that decide what becomes of the browser
session: ``completed`` and ``needs_human`` close it, ``login_required`` never
opens one, and ``approval_pending`` parks it for a resume that has to ask
Keychute about the same login step rather than opening a second request.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import httpx
import pytest

from family_assistant.config_loader import load_config
from family_assistant.config_models import AppConfig, BrowserHandoffConfig, ToolsConfig
from family_assistant.interfaces import ChatInterface
from family_assistant.llm import LLMOutput, ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.processing.types import DelegationSecurityLevel
from family_assistant.storage.database import Database, DatabaseTransaction
from family_assistant.storage.repositories.tasks import TasksRepository
from family_assistant.tools import (
    LOCAL_TOOL_REGISTRATIONS,
    LocalToolsProvider,
    PolicyEnforcingToolsProvider,
    PolicyEngine,
    ToolPolicyConfig,
    ToolPolicyDecision,
)
from family_assistant.tools import authenticated_sites as authenticated_sites_module
from family_assistant.tools import browser_backend as browser_backend_module
from family_assistant.tools import browser_dom as browser_dom_module
from family_assistant.tools.authenticated_sites import run_authenticated_site_task_tool
from family_assistant.tools.browser_backend import (
    AuthenticatedSessionSpec,
    RemoteBrowserBackend,
    authenticated_binding_for,
    release_authenticated_session,
)
from tests.mocks.mock_llm import (
    MatcherArgs,
    RuleBasedMockLLMClient,
    extract_text_from_content,
    last_real_message,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.storage.delegation_runs import AuthenticatedSiteEnvelope
    from family_assistant.storage.tasks import TaskPriority
    from family_assistant.tools.browser_backend import JsonDict
    from family_assistant.tools.types import ToolExecutionContext

pytestmark = pytest.mark.asyncio

SERVICE_URL = "http://browser-server.test:8000"
ORIGIN = "https://shop.example.test"
SITE_ID = "testsite"
DISPLAY_NAME = "Test Site"
JAR_ID = "jar_1"
JAR_GENERATION = 7
CREDENTIAL_ALIAS = "testsite"
SESSION_ID = "bs_auth_1"

CALLER_PROFILE_ID = "default_assistant"
WORKER_PROFILE_ID = "authenticated_browser_profile"
TEST_USER = "andrew"
TEST_INTERFACE = "test_interface"
OBJECTIVE = "Check this week's order."
WORKER_REPORT = "Worker report"
_RUNNING_PREFIX = f"{DISPLAY_NAME}: running."


@pytest.fixture(autouse=True)
def _service_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_HANDOFF_SERVICE_TOKEN", "test-token")
    monkeypatch.setattr(authenticated_sites_module, "_active_runs", {})
    monkeypatch.setattr(authenticated_sites_module, "_conversation_locks", {})
    monkeypatch.setattr(browser_backend_module, "_authenticated_bindings", {})


@pytest.fixture(autouse=True)
def _no_ucp_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the snapshot path off the network.

    A snapshot that lands on a new origin probes it for a UCP shopping profile
    over real HTTPS, which has nothing to do with what is under test here.
    """

    async def no_profile(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(browser_dom_module, "discover_merchant_ucp_profile", no_profile)


@pytest.fixture(scope="module")
def app_config() -> AppConfig:
    """The shipped configuration with one authenticated site enabled."""
    data = load_config("defaults.yaml").model_dump()
    data["authenticated_sites"] = {
        SITE_ID: {
            "display_name": DISPLAY_NAME,
            "jar_id": JAR_ID,
            "start_url": f"{ORIGIN}/orders",
            "authenticated_origins": [ORIGIN],
            "credential_alias": CREDENTIAL_ALIAS,
            "authorized_users": [TEST_USER],
            "caller_profiles": [CALLER_PROFILE_ID],
            "damage_envelope": "Orders only.",
        }
    }
    data["browser_handoff_config"] = {
        **data["browser_handoff_config"],
        "enabled": True,
        "service_url": SERVICE_URL,
        "auth": {"type": "bearer", "token_env": "BROWSER_HANDOFF_SERVICE_TOKEN"},
    }
    return AppConfig.model_validate(data)


@dataclass
class FakeBrowserServer:
    """browser-server, reduced to the endpoints an authenticated run touches.

    The autofill endpoint is scriptable because the whole approval flow hangs
    off what it answers, and every request is kept so the test can assert on
    what crossed the wire -- which session was closed, and which login step
    each fill named.
    """

    session_read_status: int = 200
    jar_invalidated: bool = False
    autofill_replies: list[JsonDict] = field(default_factory=list)
    requests: list[tuple[str, str, JsonDict]] = field(default_factory=list)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self._handle), timeout=5.0
        )

    def paths(self, method: str, path: str) -> list[JsonDict]:
        """Every body sent to one endpoint, in order."""
        return [
            body
            for seen_method, seen_path, body in self.requests
            if seen_method == method and seen_path == path
        ]

    @property
    def sessions_created(self) -> int:
        return len(self.paths("POST", "/v1/sessions"))

    @property
    def sessions_closed(self) -> int:
        return len(self.paths("POST", f"/v1/sessions/{SESSION_ID}/close"))

    @property
    def autofill_step_keys(self) -> list[str]:
        return [
            str(body["step_key"])
            for body in self.paths("POST", f"/v1/sessions/{SESSION_ID}/autofill")
        ]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = cast("JsonDict", json.loads(request.content) if request.content else {})
        self.requests.append((request.method, path, body))
        if path.endswith("/probe"):
            return httpx.Response(200, json={"jar_id": JAR_ID, "fresh": True})
        if path.startswith("/v1/jars/"):
            return httpx.Response(
                200,
                json={
                    "jar_id": JAR_ID,
                    "generation": JAR_GENERATION,
                    "invalidated_at": (
                        "2026-01-01T00:00:00Z" if self.jar_invalidated else None
                    ),
                },
            )
        if path == "/v1/sessions":
            return httpx.Response(
                200,
                json={
                    "session_id": SESSION_ID,
                    "authenticated_site": True,
                    "confine_origins": [ORIGIN],
                    "jar_generation": JAR_GENERATION,
                    "credential_alias": CREDENTIAL_ALIAS,
                },
            )
        if path.endswith("/agent-command"):
            return httpx.Response(200, json={"result": _command_result(body)})
        if path.endswith("/autofill/outcome"):
            return httpx.Response(200, json={"autofill_bad_password": True})
        if path.endswith("/autofill"):
            reply = (
                self.autofill_replies.pop(0)
                if self.autofill_replies
                else {"status": "filled", "filled": [{"kind": "password"}]}
            )
            return httpx.Response(200, json={**reply, "origin": ORIGIN})
        if path.endswith("/close"):
            return httpx.Response(200, json={})
        if request.method == "GET":
            if self.session_read_status != 200:
                return httpx.Response(
                    self.session_read_status, json={"detail": "unknown session"}
                )
            return httpx.Response(
                200, json={"session_id": SESSION_ID, "state": "agent_active"}
            )
        return httpx.Response(404, json={"detail": f"no fake route for {path}"})


def _command_result(body: JsonDict) -> JsonDict:
    args = cast("JsonDict", body.get("args") or {})
    if body.get("type") == "navigate":
        return {"url": args.get("url", "")}
    if body.get("type") == "snapshot":
        return {
            "url": f"{ORIGIN}/orders",
            "title": "Your orders",
            "forms": 0,
            "elements": 1,
            "next_ref": int(cast("int", args.get("next_ref", 1))) + 1,
            "roots": [{"ref": "e1", "role": "heading", "name": "Your orders"}],
        }
    return {}


@pytest.fixture
def browser_server(monkeypatch: pytest.MonkeyPatch) -> FakeBrowserServer:
    """Answer every browser-server call from the fake, on the backend's own seam.

    The tool builds its backends itself from configuration, so the injection
    seam ``RemoteBrowserBackend`` already exposes for tests is filled in by
    default here rather than reaching into the tool.
    """
    server = FakeBrowserServer()
    build = RemoteBrowserBackend.__init__

    def with_fake_client(
        backend: RemoteBrowserBackend,
        config: BrowserHandoffConfig,
        conversation_id: str,
        client: httpx.AsyncClient | None = None,
        timezone_id: str | None = None,
        authenticated: AuthenticatedSessionSpec | None = None,
    ) -> None:
        build(
            backend,
            config,
            conversation_id,
            client=client or server.client(),
            timezone_id=timezone_id,
            authenticated=authenticated,
        )

    monkeypatch.setattr(RemoteBrowserBackend, "__init__", with_fake_client)
    return server


@dataclass
class ResumeHandle:
    """The handle the caller's next turn resumes with, once it has one."""

    delegation_id: str | None = None


def _tool_call(name: str, arguments: JsonDict) -> LLMOutput:
    return LLMOutput(
        content=f"Calling {name}.",
        tool_calls=[
            ToolCallItem(
                id=f"call_{uuid.uuid4()}",
                type="function",
                function=ToolCallFunction(name=name, arguments=json.dumps(arguments)),
            )
        ],
    )


def _newest_role(kwargs: MatcherArgs) -> str | None:
    message = last_real_message(kwargs.get("messages", []))
    return message.role if message is not None else None


def _newest_text(kwargs: MatcherArgs) -> str:
    message = last_real_message(kwargs.get("messages", []))
    return extract_text_from_content(message.content) if message is not None else ""


def _caller_llm(resume: ResumeHandle) -> RuleBasedMockLLMClient:
    """A caller that asks for the site task, then reports what it got back.

    A run that is still going when the caller's wait ends comes back as
    ``running`` with a handle, and the tool's own instruction is then to call
    again with it; following that instruction is what a model is told to do.
    Only a run that genuinely outlives the inline window says ``running``: a
    run that finished or parked has settled before its row went terminal, so
    the caller's first result already carries the outcome.
    """

    def ask_for_the_site_task(_kwargs: MatcherArgs) -> LLMOutput:
        arguments: JsonDict = {"site_id": SITE_ID, "objective": OBJECTIVE}
        if resume.delegation_id is not None:
            arguments["resume"] = resume.delegation_id
        return _tool_call("run_authenticated_site_task", arguments)

    def still_running(kwargs: MatcherArgs) -> bool:
        return _newest_role(kwargs) == "tool" and _RUNNING_PREFIX in _newest_text(
            kwargs
        )

    def ask_again(kwargs: MatcherArgs) -> LLMOutput:
        return _tool_call(
            "run_authenticated_site_task",
            {
                "site_id": SITE_ID,
                "objective": OBJECTIVE,
                "resume": _resume_handle(_newest_text(kwargs)),
            },
        )

    return RuleBasedMockLLMClient(
        rules=[
            (still_running, ask_again),
            (
                lambda kwargs: _newest_role(kwargs) == "tool",
                lambda kwargs: LLMOutput(content=_newest_text(kwargs)),
            ),
            (lambda kwargs: _newest_role(kwargs) == "user", ask_for_the_site_task),
        ]
    )


def _worker_llm(tool_name: str, arguments: JsonDict) -> RuleBasedMockLLMClient:
    """A worker that makes one browser call per turn, then reports."""
    return RuleBasedMockLLMClient(
        rules=[
            (
                lambda kwargs: _newest_role(kwargs) == "tool",
                lambda kwargs: LLMOutput(
                    content=f"{WORKER_REPORT}: {_newest_text(kwargs)[:80]}"
                ),
            ),
            (
                lambda kwargs: _newest_role(kwargs) == "user",
                lambda _kwargs: _tool_call(tool_name, arguments),
            ),
        ]
    )


async def _service(
    *, profile_id: str, llm: RuleBasedMockLLMClient, app_config: AppConfig
) -> ProcessingService:
    tools_provider = PolicyEnforcingToolsProvider(
        wrapped_provider=LocalToolsProvider(registrations=LOCAL_TOOL_REGISTRATIONS),
        policy_engine=PolicyEngine.from_policy_config(
            ToolPolicyConfig(default_decision=ToolPolicyDecision.ALLOW, rules=[])
        ),
    )
    await tools_provider.get_tool_definitions()
    return ProcessingService(
        llm_client=llm,
        tools_provider=tools_provider,
        service_config=ProcessingServiceConfig(
            prompts={"system_prompt": "You are a {profile_id} assistant."},
            timezone=ZoneInfo("UTC"),
            max_history_messages=20,
            history_max_age_hours=24,
            tools_config=ToolsConfig(delegate_handoff_after_seconds=60.0),
            delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
            id=profile_id,
        ),
        context_providers=[],
        server_url="http://test.server",
        app_config=app_config,
        credential_resolvers=None,
        api_backend=None,
    )


@dataclass
class Harness:
    """A caller profile wired to a worker profile and a running task worker."""

    caller: ProcessingService
    engine: AsyncEngine
    conversation_id: str

    async def ask(self, text: str) -> str:
        result = await self.caller.handle_chat_interaction(
            db_context=Database(engine=self.engine),
            interface_type=TEST_INTERFACE,
            conversation_id=self.conversation_id,
            trigger_content_parts=[{"type": "text", "text": text}],
            trigger_interface_message_id=None,
            user_name=TEST_USER,
            chat_interface=MagicMock(spec=ChatInterface),
            request_confirmation_callback=AsyncMock(),
        )
        assert result.error_traceback is None, result.error_traceback
        assert result.text_reply is not None
        return result.text_reply


async def _harness(
    *,
    app_config: AppConfig,
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[object, object, object]],
    worker_llm: RuleBasedMockLLMClient,
    resume: ResumeHandle,
    conversation_id: str,
) -> Harness:
    caller = await _service(
        profile_id=CALLER_PROFILE_ID,
        llm=_caller_llm(resume),
        app_config=app_config,
    )
    worker = await _service(
        profile_id=WORKER_PROFILE_ID, llm=worker_llm, app_config=app_config
    )
    registry = {CALLER_PROFILE_ID: caller, WORKER_PROFILE_ID: worker}
    caller.processing_services_registry = registry
    worker.processing_services_registry = registry
    task_worker_manager(caller, MagicMock(spec=ChatInterface))
    return Harness(caller=caller, engine=db_engine, conversation_id=conversation_id)


def _resume_handle(reply: str) -> str:
    match = re.search(r"resume='([^']+)'", reply)
    assert match is not None, f"no resume handle offered in: {reply}"
    return match.group(1)


async def _envelope(
    engine: AsyncEngine, delegation_id: str
) -> AuthenticatedSiteEnvelope:
    """The run's typed outcome, read once rather than waited for.

    The session is settled before the run row goes terminal, so a caller that
    has been handed a run's outcome has been handed its envelope too.
    """
    run = await Database(engine=engine).delegation_runs.get_by_delegation_id(
        delegation_id
    )
    assert run is not None
    envelope = run["authenticated_site_json"]
    assert envelope is not None
    return envelope


async def test_a_completed_run_returns_a_typed_result_and_closes_its_session(
    app_config: AppConfig,
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[object, object, object]],
    browser_server: FakeBrowserServer,
) -> None:
    """The ordinary path: the worker looks at the page and the run settles."""
    harness = await _harness(
        app_config=app_config,
        db_engine=db_engine,
        task_worker_manager=task_worker_manager,
        worker_llm=_worker_llm("browser_snapshot", {}),
        resume=ResumeHandle(),
        conversation_id="conv-completed",
    )

    reply = await harness.ask("Please check my order.")

    assert f"{DISPLAY_NAME}: completed." in reply
    assert WORKER_REPORT in reply
    assert browser_server.sessions_created == 1
    assert browser_server.sessions_closed == 1


async def test_a_revoked_jar_reports_login_required_without_opening_a_session(
    app_config: AppConfig,
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[object, object, object]],
    browser_server: FakeBrowserServer,
) -> None:
    """A revoked login is a kill switch, so nothing is started on its behalf."""
    browser_server.jar_invalidated = True
    harness = await _harness(
        app_config=app_config,
        db_engine=db_engine,
        task_worker_manager=task_worker_manager,
        worker_llm=_worker_llm("browser_snapshot", {}),
        resume=ResumeHandle(),
        conversation_id="conv-login-required",
    )

    reply = await harness.ask("Please check my order.")

    assert f"{DISPLAY_NAME}: login_required." in reply
    assert browser_server.sessions_created == 0


async def test_a_resumed_run_asks_about_the_same_login_step(
    app_config: AppConfig,
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[object, object, object]],
    browser_server: FakeBrowserServer,
) -> None:
    """A pending approval parks the session; the resume reuses its step key.

    Reusing it is what makes the household's decision apply to the fill it was
    asked about: browser-server derives the Keychute idempotency key from the
    session and this step key, so a fresh one would open a second request for a
    step that is already being decided.
    """
    browser_server.autofill_replies = [
        {"status": "approval_pending", "request_id": "req_1"}
    ]
    resume = ResumeHandle()
    harness = await _harness(
        app_config=app_config,
        db_engine=db_engine,
        task_worker_manager=task_worker_manager,
        worker_llm=_worker_llm("browser_autofill", {"kind": "password"}),
        resume=resume,
        conversation_id="conv-approval",
    )

    parked = await harness.ask("Please sign in and check my order.")

    assert f"{DISPLAY_NAME}: approval_pending." in parked
    resume.delegation_id = _resume_handle(parked)
    envelope = await _envelope(db_engine, resume.delegation_id)
    assert envelope["status"] == "approval_pending"
    assert envelope.get("session_id") == SESSION_ID
    assert browser_server.sessions_closed == 0

    resumed = await harness.ask("They approved it, carry on.")

    assert f"{DISPLAY_NAME}: completed." in resumed
    step_keys = browser_server.autofill_step_keys
    assert len(step_keys) == 2
    assert step_keys[0] == step_keys[1]
    assert browser_server.sessions_created == 1
    assert browser_server.sessions_closed == 1


async def test_a_rejected_password_needs_a_human_and_closes_the_session(
    app_config: AppConfig,
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[object, object, object]],
    browser_server: FakeBrowserServer,
) -> None:
    """Nobody can correct a stored password from inside the session."""
    harness = await _harness(
        app_config=app_config,
        db_engine=db_engine,
        task_worker_manager=task_worker_manager,
        worker_llm=_worker_llm(
            "browser_report_login_outcome", {"outcome": "bad_password"}
        ),
        resume=ResumeHandle(),
        conversation_id="conv-needs-human",
    )

    reply = await harness.ask("Please sign in and check my order.")

    assert f"{DISPLAY_NAME}: needs_human." in reply
    assert browser_server.sessions_closed == 1


@pytest.fixture(autouse=True)
def _observe_binding_before_worker_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = TasksRepository.enqueue

    async def enqueue(
        repository: TasksRepository,
        task_id: str,
        task_type: str,
        payload: Mapping[str, object] | None = None,
        scheduled_at: datetime | None = None,
        max_retries_override: int | None = None,
        recurrence_rule: str | None = None,
        original_task_id: str | None = None,
        only_if_absent: bool = False,
        *,
        priority: TaskPriority,
    ) -> None:
        # This wrapper forwards the repository's full call surface while observing
        # the real transaction, including notification work enqueued by the worker.
        if task_type == "delegated_profile_run":
            assert payload is not None
            txn = cast("DatabaseTransaction", repository._db)
            run = await txn.delegation_runs.get_by_delegation_id(
                str(payload["delegation_id"])
            )
            assert run is not None
            envelope = run["authenticated_site_json"]
            assert envelope is not None
            assert envelope["status"] == "running"

            def verify_binding() -> None:
                binding = authenticated_binding_for(run["subconversation_id"])
                assert binding is not None
                assert binding.delegation_id == run["delegation_id"]
                assert binding.backend.session_id == envelope.get("session_id")

            txn.on_commit(verify_binding)
        await original(
            repository,
            task_id,
            task_type,
            payload,
            scheduled_at,
            max_retries_override,
            recurrence_rule,
            original_task_id,
            only_if_absent,
            priority=priority,
        )

    monkeypatch.setattr(TasksRepository, "enqueue", enqueue)


@pytest.mark.parametrize(
    "failure", ["binding", "gone_404", "gone_410", "transient_503"]
)
async def test_resume_after_losing_the_binding_fails_closed(
    failure: str,
    app_config: AppConfig,
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[object, object, object]],
    browser_server: FakeBrowserServer,
) -> None:
    browser_server.autofill_replies = [
        {"status": "approval_pending", "request_id": "req_1"}
    ]
    resume = ResumeHandle()
    harness = await _harness(
        app_config=app_config,
        db_engine=db_engine,
        task_worker_manager=task_worker_manager,
        worker_llm=_worker_llm("browser_autofill", {"kind": "password"}),
        resume=resume,
        conversation_id="conv-lost-binding",
    )
    parked = await harness.ask("Please sign in and check my order.")
    resume.delegation_id = _resume_handle(parked)
    run = await Database(engine=db_engine).delegation_runs.get_by_delegation_id(
        resume.delegation_id
    )
    assert run is not None
    if failure == "binding":
        release_authenticated_session(run["subconversation_id"])
    else:
        browser_server.session_read_status = int(failure.rsplit("_", 1)[1])

    reply = await harness.ask("They approved it, carry on.")

    if failure == "transient_503":
        envelope = await _envelope(db_engine, resume.delegation_id)
        assert envelope["status"] == "approval_pending"
        assert authenticated_binding_for(run["subconversation_id"]) is not None
        return
    assert "failed" in reply or "session for this task is gone" in reply
    assert browser_server.sessions_created == 1
    assert browser_server.sessions_closed == (1 if failure == "binding" else 0)
    assert len(browser_server.autofill_step_keys) == 1
    envelope = await _envelope(db_engine, resume.delegation_id)
    assert envelope["status"] == "failed"
    assert authenticated_binding_for(run["subconversation_id"]) is None
    browser_server.session_read_status = 200
    resume.delegation_id = None
    await harness.ask("Start a fresh order check.")
    assert browser_server.sessions_created == 2


@pytest.mark.parametrize(
    "changed", ["user_id", "processing_profile_id", "subconversation_id"]
)
async def test_other_callers_cannot_touch_a_parked_run(
    changed: str,
    app_config: AppConfig,
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[object, object, object]],
    browser_server: FakeBrowserServer,
) -> None:
    browser_server.autofill_replies = [
        {"status": "approval_pending", "request_id": "req_1"}
    ]
    resume = ResumeHandle()
    harness = await _harness(
        app_config=app_config,
        db_engine=db_engine,
        task_worker_manager=task_worker_manager,
        worker_llm=_worker_llm("browser_autofill", {"kind": "password"}),
        resume=resume,
        conversation_id="conv-other-caller",
    )
    parked = await harness.ask("Please sign in and check my order.")
    delegation_id = _resume_handle(parked)
    before = await _envelope(db_engine, delegation_id)
    requests_before = len(browser_server.requests)
    context = SimpleNamespace(
        db_context=Database(engine=db_engine),
        conversation_id=harness.conversation_id,
        interface_type=TEST_INTERFACE,
        user_id=None,
        user_name=TEST_USER,
        processing_profile_id=CALLER_PROFILE_ID,
        subconversation_id=None,
        processing_service=harness.caller,
    )
    setattr(context, changed, "another-caller")
    result = await run_authenticated_site_task_tool(
        cast("ToolExecutionContext", context),
        SITE_ID,
        OBJECTIVE,
        resume=delegation_id,
    )
    assert "No authenticated site task" in result.get_text()
    assert len(browser_server.requests) == requests_before
    assert await _envelope(db_engine, delegation_id) == before
