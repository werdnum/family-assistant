"""The atomic result of an authenticated run stopping.

A caller waiting on an authenticated run is woken by the delegation row going
terminal, and everything it needs -- the outcome, the resume handle for a
parked session -- lives in the typed envelope beside it. So the envelope has to
be there already when the row turns terminal; settling afterwards leaves a
window in which the caller is told `running` for a run that has finished.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import update

from family_assistant.config_models import BrowserHandoffConfig, RemoteA2AAuthConfig
from family_assistant.interfaces import ChatInterface
from family_assistant.llm.model_selection import ModelTierEligibility
from family_assistant.processing.types import ChatInteractionResult
from family_assistant.storage.database import Database
from family_assistant.storage.delegation_runs import delegation_runs_table
from family_assistant.storage.repositories.delegation_runs import (
    DelegationRunsRepository,
)
from family_assistant.storage.tasks import TaskPriority
from family_assistant.task_worker import TaskWorker
from family_assistant.tools.authenticated_sites import (
    _derive_status,  # noqa: PLC2701 - settlement boundary regression
)
from family_assistant.tools.browser_backend import (
    AuthenticatedSessionBinding,
    AuthenticatedSessionSpec,
    BrowserBackendError,
    RemoteBrowserBackend,
    authenticated_binding_for,
    bind_authenticated_session,
    release_authenticated_session,
)
from family_assistant.tools.types import ToolExecutionContext
from family_assistant.utils.clock import SystemClock

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.storage.delegation_runs import (
        AuthenticatedSiteEnvelope,
        DelegationLocalFailureKind,
    )
    from family_assistant.storage.repositories.delegation_runs import DelegationRunDict

pytestmark = pytest.mark.asyncio

INTERFACE_TYPE = "test_interface"
CONVERSATION_ID = "authenticated-settlement-conv"
ORIGIN = "https://shop.example.test"
SESSION_ID = "bs_auth_settle"
WORKER_REPLY = "Checked the order."


class _FakeSiteWorker:
    """The delegated browser profile, reduced to answering the turn."""

    kind = "local"

    def __init__(self) -> None:
        self.service_config = SimpleNamespace(
            id="authenticated_browser_profile",
            allowed_delegation_sources=["default_assistant"],
            tier_eligibility=ModelTierEligibility(),
        )

    async def handle_chat_interaction(self, **kwargs: Any) -> ChatInteractionResult:  # noqa: ANN401 - test fake accepts the ProcessingService keyword surface
        _ = kwargs
        return ChatInteractionResult.success(text_reply=WORKER_REPLY)


def _backend(*, unavailable: bool = False) -> RemoteBrowserBackend:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if unavailable:
                return httpx.Response(503, json={"detail": "browser unavailable"})
            return httpx.Response(
                200, json={"session_id": SESSION_ID, "state": "agent_active"}
            )
        return httpx.Response(200, json={})

    backend = RemoteBrowserBackend(
        BrowserHandoffConfig(
            enabled=True,
            service_url="http://browser-server.test:8000",
            auth=RemoteA2AAuthConfig(
                type="bearer", token_env="BROWSER_HANDOFF_SERVICE_TOKEN"
            ),
        ),
        CONVERSATION_ID,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0),
        authenticated=AuthenticatedSessionSpec(
            site_id="testsite",
            jar_id=None,
            confine_origins=frozenset({ORIGIN}),
            credential_alias=None,
        ),
    )
    backend.adopt_session(SESSION_ID)
    return backend


@pytest.fixture(autouse=True)
def _service_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_HANDOFF_SERVICE_TOKEN", "test-token")


@pytest.fixture
def subconversation_id() -> Iterator[str]:
    sub_id = str(uuid.uuid4())
    yield sub_id
    release_authenticated_session(sub_id)


def _context(
    db: Database, processing_service: object, chat_interface: ChatInterface
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type=INTERFACE_TYPE,
        conversation_id=CONVERSATION_ID,
        user_name="andrew",
        user_id="andrew",
        turn_id="turn-settlement",
        db_context=db,
        task_priority=TaskPriority.INTERACTIVE,
        processing_service=cast("Any", processing_service),
        clock=SystemClock(),
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
        chat_interface=chat_interface,
        chat_interfaces={INTERFACE_TYPE: chat_interface},
    )


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "session_read",
        "persistence",
        "missing_binding",
        "cleanup",
        "cleanup_loses",
    ],
)
async def test_the_envelope_is_published_with_the_terminal_run_row(
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    subconversation_id: str,
    failure: str | None,
) -> None:
    """What the terminal row wakes up must already be able to read the outcome."""
    db = Database(engine=db_engine)
    delegation_id = f"delegation_{uuid.uuid4().hex}"
    await db.delegation_runs.create_run({
        "delegation_id": delegation_id,
        "task_id": f"task_{uuid.uuid4().hex}",
        "source_profile_id": "default_assistant",
        "target_service_id": "authenticated_browser_profile",
        "interface_type": INTERFACE_TYPE,
        "conversation_id": CONVERSATION_ID,
        "subconversation_id": subconversation_id,
        "request_text": "Check this week's order.",
        "content_parts_json": [{"type": "text", "text": "Check this week's order."}],
        "user_name": "andrew",
        "user_id": "andrew",
    })
    running: AuthenticatedSiteEnvelope = {
        "site_id": "testsite",
        "status": "running",
        "session_id": SESSION_ID,
    }
    await db.delegation_runs.set_authenticated_site_state(delegation_id, running)
    if failure in {"cleanup", "cleanup_loses"}:
        await db.execute(
            update(delegation_runs_table)
            .where(delegation_runs_table.c.delegation_id == delegation_id)
            .values(created_at=SystemClock().now() - timedelta(hours=2))
        )
    bind_authenticated_session(
        subconversation_id,
        AuthenticatedSessionBinding(
            site_id="testsite",
            delegation_id=delegation_id,
            backend=_backend(unavailable=failure == "session_read"),
        ),
    )

    if failure == "missing_binding":
        release_authenticated_session(subconversation_id)
    seen_when_terminal: list[AuthenticatedSiteEnvelope | None] = []
    mark_completed = DelegationRunsRepository.mark_completed

    async def observe_then_mark(
        self: DelegationRunsRepository,
        *,
        delegation_id: str,
        result_text: str | None,
        result_attachment_ids: list[str],
        completed_at: datetime,
        authenticated_site_state: AuthenticatedSiteEnvelope | None = None,
    ) -> DelegationRunDict | None:
        if failure == "persistence":
            raise OSError("database unavailable")
        terminal = await mark_completed(
            self,
            delegation_id=delegation_id,
            result_text=result_text,
            result_attachment_ids=result_attachment_ids,
            completed_at=completed_at,
            authenticated_site_state=authenticated_site_state,
        )
        if terminal is not None:
            seen_when_terminal.append(terminal["authenticated_site_json"])
        return terminal

    monkeypatch.setattr(DelegationRunsRepository, "mark_completed", observe_then_mark)
    mark_failed = DelegationRunsRepository.mark_failed

    async def observe_then_fail(
        self: DelegationRunsRepository,
        *,
        delegation_id: str,
        error: str,
        completed_at: datetime,
        local_failure_kind: DelegationLocalFailureKind | None = None,
        authenticated_site_state: AuthenticatedSiteEnvelope | None = None,
    ) -> DelegationRunDict | None:
        if failure == "cleanup_loses":
            winner = await mark_completed(
                self,
                delegation_id=delegation_id,
                result_text=WORKER_REPLY,
                result_attachment_ids=[],
                completed_at=completed_at,
                authenticated_site_state={**running, "status": "handoff_pending"},
            )
            assert winner is not None
            seen_when_terminal.append(winner["authenticated_site_json"])
        terminal = await mark_failed(
            self,
            delegation_id=delegation_id,
            error=error,
            completed_at=completed_at,
            local_failure_kind=local_failure_kind,
            authenticated_site_state=authenticated_site_state,
        )
        if terminal is not None:
            seen_when_terminal.append(terminal["authenticated_site_json"])
        return terminal

    monkeypatch.setattr(DelegationRunsRepository, "mark_failed", observe_then_fail)

    target = _FakeSiteWorker()
    processing_service = SimpleNamespace(
        service_config=SimpleNamespace(
            id="cleanup_worker"
            if failure in {"cleanup", "cleanup_loses"}
            else "default_assistant"
        ),
        processing_services_registry={"authenticated_browser_profile": target},
        home_assistant_client=None,
        attachment_registry=None,
    )
    chat_interface = cast("ChatInterface", AsyncMock(spec=ChatInterface))
    cast("AsyncMock", chat_interface.send_message).return_value = "delivered"
    exec_context = _context(db, processing_service, chat_interface)
    worker = TaskWorker(
        processing_service=cast("Any", processing_service),
        chat_interface=chat_interface,
        calendar_config={},
        timezone=ZoneInfo("UTC"),
        embedding_generator=MagicMock(),
        engine=db_engine,
    )

    async def execute() -> None:
        if failure in {"cleanup", "cleanup_loses"}:
            await worker.handle_delegation_run_cleanup(
                exec_context, {"running_timeout_seconds": 60.0}
            )
            return
        await worker.handle_delegated_profile_run(
            exec_context,
            {
                "delegation_id": delegation_id,
                "interface_type": INTERFACE_TYPE,
                "conversation_id": CONVERSATION_ID,
                "user_name": "andrew",
            },
        )

    if failure in {"session_read", "persistence"}:
        with pytest.raises((BrowserBackendError, OSError)):
            await execute()
        run = await db.delegation_runs.get_by_delegation_id(delegation_id)
        assert run is not None
        assert run["status"] == "running"
        assert run["authenticated_site_json"] == running
        assert not seen_when_terminal
        return
    await execute()

    if failure == "cleanup_loses":
        run = await db.delegation_runs.get_by_delegation_id(delegation_id)
        assert run is not None
        assert run["status"] == "completed"
        assert run["authenticated_site_json"] == {
            **running,
            "status": "handoff_pending",
        }
        binding = authenticated_binding_for(subconversation_id)
        assert binding is not None
        assert binding.backend.session_id == SESSION_ID
        return

    assert len(seen_when_terminal) == 1
    settled = seen_when_terminal[0]
    assert settled is not None
    assert settled["status"] == (
        "failed" if failure in {"missing_binding", "cleanup"} else "completed"
    )
    # The reply and its authenticated summary come from the same result.
    assert settled.get("summary") == ("" if failure == "cleanup" else WORKER_REPLY)
    run = await db.delegation_runs.get_by_delegation_id(delegation_id)
    assert run is not None
    assert run["status"] == ("failed" if failure == "cleanup" else "completed")
    if failure in {"cleanup", "cleanup_loses"}:
        assert authenticated_binding_for(subconversation_id) is None


@pytest.mark.parametrize(
    "state", [None, [], {}, {"state": "new_parked_state"}, {"state": "expired"}]
)
async def test_unknown_browser_state_cannot_settle_successfully(state: object) -> None:
    binding = AuthenticatedSessionBinding(
        site_id="testsite", delegation_id="run", backend=_backend()
    )
    with pytest.raises(BrowserBackendError, match="unrecognized browser session state"):
        _derive_status(binding, state)
