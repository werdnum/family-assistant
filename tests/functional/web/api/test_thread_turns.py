"""A web thread's first turn runs live in the thread.

See docs/design/one-thread-chat-ux.md: the turn streams like any turn the user
sends, and a message sent while it runs steers it rather than starting another.

The run is created here without its queue task and handed to a worker directly,
so the API app's own worker pool -- wired to a different app state -- never
picks it up.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update

from family_assistant.config_models import AppConfig, ConversationThreadsConfig
from family_assistant.interfaces import ChatInterface
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService
from family_assistant.storage import delegation_runs_table, tasks_table
from family_assistant.storage.database import Database
from family_assistant.storage.tasks import TaskPriority
from family_assistant.task_worker import TaskWorker
from family_assistant.threads.boundary import (
    AssistantRequest,
    BoundaryDestination,
    copy_across_boundary,
)
from family_assistant.tools.types import ToolExecutionContext
from family_assistant.utils.clock import SystemClock
from family_assistant.web.thread_turns import WebThreadTurnRunner
from tests.helpers import wait_for_condition
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

    from fastapi import FastAPI
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.web.conversation_stream_hub import ConversationStreamHub

TARGET = "complex_tasks"
OWNER = "test_user"
THREAD = "web_conv_thread"
THREAD_REPLY = "Here are three quotes."


class _EnteredGate(asyncio.Event):
    """A response gate that says when a model call first waits on it."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()

    async def wait(self) -> Literal[True]:
        self.entered.set()
        return await super().wait()


@dataclass
class LiveThread:
    client: AsyncClient
    db: Database
    target_gate: asyncio.Event
    turn_id: str

    async def wait_until_running(self) -> None:
        async def running() -> bool:
            rows = await self.db.fetch_all(
                select(delegation_runs_table.c.status).where(
                    delegation_runs_table.c.run_kind == "thread"
                )
            )
            return bool(rows) and rows[0]["status"] == "running"

        await wait_for_condition(running, description="the thread run to start")


async def _create_thread_run(
    db: Database, home: str, target: ProcessingService
) -> tuple[str, str]:
    """A thread of ``home`` with its seed and a queued run, as start_thread makes."""
    delegation_id = f"delegation_{uuid.uuid4().hex}"
    turn_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    await db.conversation_threads.create_thread(
        conversation_id=THREAD,
        home_conversation_id=home,
        interface_type="web",
        owner_user_id=OWNER,
        profile_id=TARGET,
        title="Hot water system quotes",
        title_taint=None,
        anchor_message_id=None,
        delegation_id=delegation_id,
        now=now,
    )
    await copy_across_boundary(
        db,
        destination=BoundaryDestination(
            interface_type="web",
            conversation_id=THREAD,
            profile=target.service_config,
            user_id=OWNER,
        ),
        payload=AssistantRequest(text="Find three plumbers.", taint=None),
        turn_id=turn_id,
        now=now,
    )
    await db.delegation_runs.create_run({
        "delegation_id": delegation_id,
        "task_id": f"task_{delegation_id}",
        "source_profile_id": "default_assistant",
        "target_service_id": TARGET,
        "interface_type": "web",
        "conversation_id": THREAD,
        "user_id": OWNER,
        "user_name": "Test User",
        "subconversation_id": None,
        "run_kind": "thread",
        "turn_id": turn_id,
        "request_text": "Find three plumbers.",
        "content_parts_json": [{"type": "text", "text": "Find three plumbers."}],
    })
    return delegation_id, turn_id


@dataclass
class QueuedThread:
    """A thread whose first run is queued, and a worker ready to run it."""

    client: AsyncClient
    db: Database
    target_gate: asyncio.Event
    target_llm: RuleBasedMockLLMClient
    target_service: ProcessingService
    delegation_id: str
    turn_id: str
    worker: TaskWorker
    context: ToolExecutionContext
    hub: ConversationStreamHub

    async def run_worker(self) -> None:
        await self.worker.handle_delegated_profile_run(
            self.context,
            {
                "delegation_id": self.delegation_id,
                "interface_type": "web",
                "conversation_id": THREAD,
                "user_name": "Test User",
            },
        )


@pytest.fixture
async def queued_thread(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    api_test_processing_service: ProcessingService,
    db_engine: AsyncEngine,
) -> QueuedThread:
    app_config = AppConfig(
        database_url=app_fixture.state.config.database_url,
        conversation_threads=ConversationThreadsConfig(enabled=True),
    )
    app_fixture.state.config = app_config
    target_gate = asyncio.Event()
    target_llm = RuleBasedMockLLMClient(
        rules=[],
        default_response=LLMOutput(content=THREAD_REPLY),
        response_gate=target_gate,
    )
    target_service = ProcessingService(
        llm_client=target_llm,
        tools_provider=api_test_processing_service.tools_provider,
        service_config=replace(api_test_processing_service.service_config, id=TARGET),
        context_providers=[],
        server_url="http://testserver",
        app_config=app_config,
    )
    registry = {TARGET: target_service}
    target_service.processing_services_registry = registry
    api_test_processing_service.processing_services_registry = registry
    app_fixture.state.processing_services = registry
    db = Database(engine=db_engine)
    home = (await api_test_client.get("/api/v1/chat/home")).json()["conversation_id"]
    delegation_id, turn_id = await _create_thread_run(db, home, target_service)
    worker = TaskWorker(
        processing_service=api_test_processing_service,
        chat_interface=MagicMock(spec=ChatInterface),
        calendar_config={},
        timezone=ZoneInfo("UTC"),
        embedding_generator=MagicMock(),
        engine=db_engine,
        stream_hub=app_fixture.state.conversation_stream_hub,
        thread_turn_runner=WebThreadTurnRunner(app_fixture.state),
    )
    context = ToolExecutionContext(
        interface_type="web",
        conversation_id=THREAD,
        user_name="Test User",
        user_id=OWNER,
        turn_id=None,
        db_context=db,
        task_priority=TaskPriority.INTERACTIVE,
        processing_service=api_test_processing_service,
        clock=SystemClock(),
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )
    return QueuedThread(
        client=api_test_client,
        db=db,
        target_gate=target_gate,
        target_llm=target_llm,
        target_service=target_service,
        delegation_id=delegation_id,
        turn_id=turn_id,
        worker=worker,
        context=context,
        hub=app_fixture.state.conversation_stream_hub,
    )


@pytest.fixture
async def live_thread(queued_thread: QueuedThread) -> AsyncGenerator[LiveThread]:
    handler = asyncio.create_task(queued_thread.run_worker())
    yield LiveThread(
        client=queued_thread.client,
        db=queued_thread.db,
        target_gate=queued_thread.target_gate,
        turn_id=queued_thread.turn_id,
    )
    queued_thread.target_gate.set()
    await handler


@pytest.mark.asyncio
async def test_the_first_turn_streams_in_the_thread(live_thread: LiveThread) -> None:
    await live_thread.wait_until_running()
    live_thread.target_gate.set()

    response = await live_thread.client.get(
        f"/api/v1/chat/conversations/{THREAD}/stream",
        params={"from_seq": 0, "follow": "false"},
    )

    assert THREAD_REPLY in "".join(
        json.loads(line.removeprefix("data: ")).get("content", "")
        for line in response.text.splitlines()
        if line.startswith("data: ") and '"content"' in line
    )


@pytest.mark.asyncio
async def test_a_message_while_the_first_turn_runs_is_sent_to_steer_it(
    live_thread: LiveThread,
) -> None:
    await live_thread.wait_until_running()

    response = await live_thread.client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": str(uuid.uuid4()),
            "conversation_id": THREAD,
            "prompt": "Gas, please.",
            "interface_type": "web",
        },
    )

    assert (response.status_code, response.json()["detail"]["active_turn_id"]) == (
        409,
        live_thread.turn_id,
    )


async def _send_before_the_first_turn(queued_thread: QueuedThread) -> None:
    queued_thread.target_gate.set()
    turn = await queued_thread.client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": str(uuid.uuid4()),
            "conversation_id": THREAD,
            "prompt": "Gas, please.",
            "interface_type": "web",
        },
    )
    assert turn.status_code == 200
    await queued_thread.client.get(
        f"/api/v1/chat/conversations/{THREAD}/stream",
        params={"from_seq": 0, "follow": "false"},
    )


@pytest.mark.asyncio
async def test_a_message_before_the_first_turn_becomes_that_turn(
    queued_thread: QueuedThread,
) -> None:
    await _send_before_the_first_turn(queued_thread)

    [call] = queued_thread.target_llm.get_calls()
    texts = [str(message.content) for message in call["kwargs"]["messages"]]
    request_at = next(
        i for i, text in enumerate(texts) if "Find three plumbers." in text
    )
    message_at = next(i for i, text in enumerate(texts) if "Gas, please." in text)
    assert request_at < message_at


@pytest.mark.asyncio
async def test_the_absorbed_run_does_not_run_afterwards(
    queued_thread: QueuedThread,
) -> None:
    await _send_before_the_first_turn(queued_thread)

    await queued_thread.run_worker()

    assert len(queued_thread.target_llm.get_calls()) == 1


@pytest.mark.asyncio
async def test_a_first_turn_that_errors_fails_the_run(
    queued_thread: QueuedThread, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_stream(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        async def stream() -> AsyncIterator[object]:
            raise RuntimeError("provider unavailable")
            yield  # pragma: no cover - makes this an async generator

        return stream()

    monkeypatch.setattr(
        queued_thread.target_llm, "generate_response_stream", failing_stream
    )
    queued_thread.target_gate.set()

    await queued_thread.run_worker()

    [run] = await queued_thread.db.fetch_all(
        select(delegation_runs_table.c.status).where(
            delegation_runs_table.c.run_kind == "thread"
        )
    )
    assert run["status"] == "failed"


@pytest.mark.asyncio
async def test_a_first_turn_that_outlasts_its_budget_fails_the_run_as_timed_out(
    queued_thread: QueuedThread,
) -> None:
    queued_thread.worker.handler_timeout_overrides["delegated_profile_run"] = 1.0

    await queued_thread.run_worker()

    [run] = await queued_thread.db.fetch_all(
        select(
            delegation_runs_table.c.status,
            delegation_runs_table.c.local_failure_kind,
            delegation_runs_table.c.error,
        ).where(delegation_runs_table.c.run_kind == "thread")
    )
    assert (run["status"], run["local_failure_kind"]) == ("failed", "timeout")
    assert "timed out" in run["error"]


@pytest.mark.asyncio
async def test_the_first_turn_is_reviewed_against_the_threads_request(
    queued_thread: QueuedThread, monkeypatch: pytest.MonkeyPatch
) -> None:
    triggers: list[object] = []
    original = queued_thread.target_service.handle_chat_interaction_stream

    def spy(*args: object, **kwargs: object) -> AsyncIterator[object]:
        triggers.append(kwargs.get("tool_call_review_trigger"))
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        queued_thread.target_service, "handle_chat_interaction_stream", spy
    )
    queued_thread.target_gate.set()

    await queued_thread.run_worker()

    assert [trigger is not None for trigger in triggers] == [True]


@pytest.mark.asyncio
async def test_a_run_waiting_on_a_busy_thread_is_rescheduled(
    queued_thread: QueuedThread,
) -> None:
    await queued_thread.hub.start_turn(
        THREAD,
        turn_id="another-turn",
        user_id=OWNER,
        started_at=datetime.now(UTC),
        reject_if_running=True,
    )

    await queued_thread.run_worker()

    [run] = await queued_thread.db.fetch_all(
        select(delegation_runs_table.c.status).where(
            delegation_runs_table.c.run_kind == "thread"
        )
    )
    rescheduled = await queued_thread.db.fetch_all(
        select(tasks_table.c.task_id).where(
            tasks_table.c.task_type == "delegated_profile_run"
        )
    )
    assert (run["status"], len(rescheduled)) == ("queued", 1)


@pytest.mark.asyncio
async def test_stopping_the_first_turn_settles_the_run(
    queued_thread: QueuedThread,
) -> None:
    handler = asyncio.create_task(queued_thread.run_worker())
    live = LiveThread(
        client=queued_thread.client,
        db=queued_thread.db,
        target_gate=queued_thread.target_gate,
        turn_id=queued_thread.turn_id,
    )
    await live.wait_until_running()

    await queued_thread.client.post(
        f"/api/v1/chat/turns/{queued_thread.turn_id}/cancel",
        json={"conversation_id": THREAD},
    )
    await handler

    [run] = await queued_thread.db.fetch_all(
        select(delegation_runs_table.c.status).where(
            delegation_runs_table.c.run_kind == "thread"
        )
    )
    assert run["status"] == "completed"


@pytest.mark.asyncio
async def test_the_first_turn_gets_the_attachments_it_was_started_with(
    queued_thread: QueuedThread, monkeypatch: pytest.MonkeyPatch
) -> None:
    parts = [
        {"type": "text", "text": "Find three plumbers."},
        {"type": "attachment", "attachment_id": "attachment-1"},
    ]
    await queued_thread.db.execute(
        update(delegation_runs_table)
        .where(delegation_runs_table.c.delegation_id == queued_thread.delegation_id)
        .values(content_parts_json=parts)
    )
    received: list[object] = []

    def spy(*_args: object, **kwargs: object) -> AsyncIterator[object]:
        received.append(kwargs.get("trigger_content_parts"))

        async def nothing() -> AsyncIterator[object]:
            return
            yield  # pragma: no cover - makes this an async generator

        return nothing()

    monkeypatch.setattr(
        queued_thread.target_service, "handle_chat_interaction_stream", spy
    )

    await queued_thread.run_worker()

    assert received == [parts]


@pytest.mark.asyncio
async def test_cancelling_the_worker_stops_the_turn(
    queued_thread: QueuedThread,
) -> None:
    # The run reads as running once claimed, a step before the turn itself
    # starts; cancelling in between abandons a turn that never began. Wait
    # for the model call so the cancellation lands on a turn in progress.
    gate = _EnteredGate()
    queued_thread.target_llm.response_gate = gate
    handler = asyncio.create_task(queued_thread.run_worker())

    await asyncio.wait_for(gate.entered.wait(), timeout=30)
    handler.cancel()
    await asyncio.wait([handler])

    record = queued_thread.hub.get_turn(THREAD, queued_thread.turn_id)
    assert record is not None
    assert record.status != "running"


@pytest.mark.asyncio
async def test_a_streamed_turn_that_finishes_the_thread_echoes_into_home(
    queued_thread: QueuedThread,
    api_test_processing_service: ProcessingService,
) -> None:
    """A thread's turns on the web stream; the echo follows them there too."""
    registry = {
        TARGET: queued_thread.target_service,
        "default_assistant": api_test_processing_service,
    }
    queued_thread.target_service.processing_services_registry = registry
    queued_thread.target_llm.rules = [
        (
            lambda kwargs: getattr(kwargs["messages"][-1], "role", None) == "tool",
            lambda _kwargs: LLMOutput(content=THREAD_REPLY),
        ),
        (
            lambda _kwargs: True,
            lambda _kwargs: LLMOutput(
                content=None,
                tool_calls=[
                    ToolCallItem(
                        id="call_finish",
                        type="function",
                        function=ToolCallFunction(
                            name="finish_thread", arguments='{"outcome": "done"}'
                        ),
                    )
                ],
            ),
        ),
    ]
    queued_thread.target_gate.set()

    await queued_thread.run_worker()

    thread = await queued_thread.db.conversation_threads.get_thread(THREAD)
    assert thread is not None and thread.close is not None
    assert thread.close.echo_message_id is not None
    echo = await queued_thread.db.message_history.get_row_by_internal_id(
        thread.close.echo_message_id
    )
    assert echo is not None
    assert (
        echo["conversation_id"],
        echo["content"],
        echo["echoed_from_conversation_id"],
    ) == (thread.home_conversation_id, THREAD_REPLY, THREAD)
