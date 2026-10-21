"""With threads on, a "new conversation" wake starts a thread off its owner's Home.

Each firing gets a thread of its own, through the same creation path as every
other thread, so its output is a card in Home rather than an unlinked
conversation. The wake's turn runs in the thread on the profile it fires on,
which the thread is pinned to.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update

from family_assistant.actions import ActionType, execute_action
from family_assistant.config_models import (
    AppConfig,
    ConversationThreadsConfig,
    ToolsConfig,
)
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.interfaces import ChatInterface
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import UserMessage
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.processing.quiet_turn import END_TURN_QUIETLY_TOOL_NAME
from family_assistant.security.taint import (
    SourceTrustTier,
    TurnTaintState,
    machine_authored_taint_metadata,
)
from family_assistant.storage.database import Database
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.tasks import tasks_table
from family_assistant.task_worker import (
    TaskWorker,
    handle_llm_callback,
    handle_script_execution,
)
from family_assistant.threads.home import find_home
from family_assistant.threads.listing import list_home_threads
from family_assistant.threads.wakes import discard_wake_thread, ensure_wake_thread
from family_assistant.tools.automations import create_automation_tool
from family_assistant.tools.infrastructure import (
    CompositeToolsProvider,
    LocalToolsProvider,
)
from family_assistant.tools.types import ToolExecutionContext
from family_assistant.web.web_chat_interface import WebChatInterface
from tests.helpers import wait_for_condition, wait_for_tasks_to_complete
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.services.notifier import NotificationMetadata
    from family_assistant.utils.clock import MockClock

SOURCE_CONVERSATION_ID = "web_conv_source"
OWNER = "alice@example.com"
REPLY = "Here is your briefing."


def _processing_service(
    *,
    threads: bool = True,
    profile_id: str = "default_assistant",
    web_history_budget_chars: int | None = None,
    llm_client: RuleBasedMockLLMClient | None = None,
) -> ProcessingService:
    tools_provider = CompositeToolsProvider(
        providers=[LocalToolsProvider(definitions=[], implementations={})]
    )
    return ProcessingService(
        llm_client=llm_client
        or RuleBasedMockLLMClient(rules=[], default_response=LLMOutput(content=REPLY)),
        tools_provider=tools_provider,
        service_config=ProcessingServiceConfig(
            id=profile_id,
            prompts={"system_prompt": "Assistant"},
            timezone=ZoneInfo("UTC"),
            history_budget_chars=0,
            history_min_turns=0,
            history_max_age_hours=1,
            web_history_budget_chars=web_history_budget_chars,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.BLOCKED,
        ),
        app_config=AppConfig(
            conversation_threads=ConversationThreadsConfig(enabled=threads)
        ),
        context_providers=[],
        server_url=None,
    )


async def _start_worker(
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
    service: ProcessingService,
    chat_interface: ChatInterface | None = None,
) -> tuple[ChatInterface, asyncio.Event]:
    await service.tools_provider.get_tool_definitions()
    if chat_interface is None:
        mock_interface = AsyncMock(spec=ChatInterface)
        mock_interface.send_message.return_value = "sent_message_id"
        chat_interface = mock_interface
    task_worker, new_task_event, _ = task_worker_manager(
        processing_service=service,
        chat_interface=chat_interface,
        register_delegation_handler=False,
    )
    task_worker.register_task_handler("llm_callback", handle_llm_callback)
    task_worker.register_task_handler("script_execution", handle_script_execution)
    return chat_interface, new_task_event


def _exec_context(db: Database, service: ProcessingService) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="web",
        conversation_id=SOURCE_CONVERSATION_ID,
        user_name="Alice",
        user_id=OWNER,
        turn_id="creating_turn",
        db_context=db,
        processing_service=service,
        processing_profile_id="default_assistant",
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


def _sent_conversation_ids(chat_interface: ChatInterface) -> list[str]:
    assert isinstance(chat_interface, AsyncMock)
    return [
        call.kwargs["conversation_id"]
        for call in chat_interface.send_message.call_args_list
    ]


async def _fire_pending(
    db_engine: AsyncEngine,
    clock: MockClock,
    new_task_event: asyncio.Event,
    task_type: str,
    *,
    allow_failures: bool = False,
) -> None:
    """Make every pending task of ``task_type`` due on the worker's clock, then wait."""
    db = Database(engine=db_engine)
    rows = await db.fetch_all(
        select(tasks_table.c.task_id).where(
            tasks_table.c.task_type == task_type,
            tasks_table.c.status == "pending",
        )
    )
    task_ids = {row["task_id"] for row in rows}
    assert task_ids, f"no pending {task_type} task"
    await db.execute(
        update(tasks_table)
        .where(tasks_table.c.task_id.in_(task_ids))
        .values(scheduled_at=clock.now())
    )
    new_task_event.set()
    await wait_for_tasks_to_complete(
        db_engine, task_ids=task_ids, allow_failures=allow_failures
    )


async def _wake_in_new_conversation(db: Database) -> None:
    await execute_action(
        db_ctx=db,
        action_type=ActionType.WAKE_LLM,
        action_config={"context": "Send the morning briefing", "conversation": "new"},
        conversation_id=SOURCE_CONVERSATION_ID,
        interface_type="web",
        created_by_user_id=OWNER,
    )


async def _home(db: Database, service: ProcessingService) -> str:
    home = await find_home(db, service.app_config, user_id=OWNER, interface_type="web")
    assert home is not None, "the wake designated no Home"
    return home


async def _profiles_of_turn_rows(db: Database, conversation_id: str) -> set[str]:
    rows = await db.fetch_all(
        select(message_history_table.c.processing_profile_id).where(
            message_history_table.c.conversation_id == conversation_id
        )
    )
    return {row["processing_profile_id"] for row in rows}


def _dated(base: str, clock: MockClock) -> str:
    when = clock.now()
    return f"{base} · {when.day} {when:%b}"


@pytest.mark.asyncio
async def test_a_new_conversation_automation_starts_a_thread_off_home(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
    mock_clock: MockClock,
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service()
    chat_interface, new_task_event = await _start_worker(task_worker_manager, service)
    result = await create_automation_tool(
        exec_context=_exec_context(db, service),
        name="Morning briefing",
        automation_type="schedule",
        trigger_config={"recurrence_rule": "FREQ=DAILY;BYHOUR=7;BYMINUTE=0"},
        action_type="wake_llm",
        action_config={"context": "Send the morning briefing", "conversation": "new"},
    )
    assert "Created schedule automation" in result.get_text(), result.get_text()

    await _fire_pending(db_engine, mock_clock, new_task_event, "llm_callback")

    [thread_id] = _sent_conversation_ids(chat_interface)
    home = await _home(db, service)
    [card] = await list_home_threads(
        db, home, turn_running=lambda _conversation_id: False, viewer=None
    )
    assert card.conversation_id == thread_id
    assert card.title == _dated("Morning briefing", mock_clock)
    assert card.profile_id == "default_assistant"
    assert card.preview == REPLY
    assert card.seed_request is None
    assert await _profiles_of_turn_rows(db, thread_id) == {"default_assistant"}


@pytest.mark.asyncio
async def test_each_firing_starts_its_own_thread(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service()
    chat_interface, new_task_event = await _start_worker(task_worker_manager, service)

    await _wake_in_new_conversation(db)
    await _wake_in_new_conversation(db)
    new_task_event.set()
    await wait_for_tasks_to_complete(db_engine, task_types={"llm_callback"})

    sent = _sent_conversation_ids(chat_interface)
    threads = await db.conversation_threads.list_for_home(await _home(db, service))
    assert len(set(sent)) == 2
    assert {thread.conversation_id for thread in threads} == set(sent)


@pytest.mark.asyncio
async def test_a_retried_wake_keeps_its_one_thread(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
    mock_clock: MockClock,
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service()
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.side_effect = [
        ConnectionError("push gateway down"),
        "sent_message_id",
    ]
    _, new_task_event = await _start_worker(
        task_worker_manager, service, chat_interface
    )

    # Due later, so the first attempt runs only once the test has watched for it.
    await execute_action(
        db_ctx=db,
        action_type=ActionType.WAKE_LLM,
        action_config={"context": "Send the morning briefing", "conversation": "new"},
        conversation_id=SOURCE_CONVERSATION_ID,
        interface_type="web",
        created_by_user_id=OWNER,
        scheduled_at=mock_clock.now() + timedelta(days=1),
    )
    [task] = await db.fetch_all(
        select(tasks_table.c.task_id).where(tasks_table.c.task_type == "llm_callback")
    )
    task_id = task["task_id"]
    await db.execute(
        update(tasks_table)
        .where(tasks_table.c.task_id == task_id)
        .values(scheduled_at=mock_clock.now())
    )
    new_task_event.set()
    await _wait_for_retry(db, task_id)
    # The retry is scheduled on the worker's frozen clock; bring it due. The
    # first attempt's error stays recorded on the task, so allow it.
    await _fire_pending(
        db_engine, mock_clock, new_task_event, "llm_callback", allow_failures=True
    )

    status = await db.fetch_one(
        select(tasks_table.c.status).where(tasks_table.c.task_id == task_id)
    )
    assert status is not None and status["status"] == "done"
    first, second = _sent_conversation_ids(chat_interface)
    threads = await db.conversation_threads.list_for_home(await _home(db, service))
    assert first == second
    assert [thread.conversation_id for thread in threads] == [first]


async def _wait_for_retry(db: Database, task_id: str) -> None:
    """Wait until the task's first attempt has failed and been rescheduled."""

    async def retried() -> bool:
        row = await db.fetch_one(
            select(tasks_table.c.retry_count, tasks_table.c.status).where(
                tasks_table.c.task_id == task_id
            )
        )
        return (
            row is not None and row["retry_count"] == 1 and row["status"] == "pending"
        )

    await wait_for_condition(retried, description=f"a retry of task {task_id}")


@pytest.mark.asyncio
async def test_a_script_wake_in_a_new_conversation_starts_a_thread(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
    mock_clock: MockClock,
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service()
    chat_interface, new_task_event = await _start_worker(task_worker_manager, service)

    await execute_action(
        db_ctx=db,
        action_type=ActionType.SCRIPT,
        action_config={
            "script_code": (
                'wake_llm("Briefing time\\nUse the calendar", new_conversation=True)\n'
            )
        },
        conversation_id=SOURCE_CONVERSATION_ID,
        interface_type="web",
        processing_profile_id="default_assistant",
        created_by_user_id=OWNER,
    )
    new_task_event.set()
    await wait_for_tasks_to_complete(
        db_engine, task_types={"script_execution", "llm_callback"}
    )

    [thread_id] = _sent_conversation_ids(chat_interface)
    [thread] = await db.conversation_threads.list_for_home(await _home(db, service))
    assert thread.conversation_id == thread_id
    assert thread.title == _dated("Briefing time", mock_clock)
    assert thread.profile_id == "default_assistant"


@pytest.mark.asyncio
async def test_an_event_wake_thread_is_pinned_to_the_profile_it_ran_on(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
    mock_clock: MockClock,
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service()
    event_service = _processing_service(profile_id="event_handler")
    registry = {"default_assistant": service, "event_handler": event_service}
    service.processing_services_registry = registry
    event_service.processing_services_registry = registry
    chat_interface, new_task_event = await _start_worker(task_worker_manager, service)
    listener_id = await db.events.create_event_listener(
        name="Garage door",
        source_id="home_assistant",
        match_conditions={"entity_id": "cover.garage"},
        conversation_id=SOURCE_CONVERSATION_ID,
        interface_type="web",
        action_config={"context": "The garage door opened", "conversation": "new"},
        created_by_user_id=OWNER,
    )

    # As the event processor fires a wake_llm listener.
    await execute_action(
        db_ctx=db,
        action_type=ActionType.WAKE_LLM,
        action_config={"context": "The garage door opened", "conversation": "new"},
        conversation_id=SOURCE_CONVERSATION_ID,
        interface_type="web",
        context={
            "trigger": "Event listener 'Garage door' matched",
            "listener_id": listener_id,
            "source": "home_assistant",
            "event_data": {"entity_id": "cover.garage", "state": "open"},
        },
        processing_profile_id="event_handler",
        created_by_user_id=OWNER,
        tool_call_review_trigger_type="event_listener",
        tool_call_review_trigger_definition="The garage door opened",
        tool_call_review_trigger_payload_present=True,
    )
    new_task_event.set()
    await wait_for_tasks_to_complete(db_engine, task_types={"llm_callback"})

    [thread_id] = _sent_conversation_ids(chat_interface)
    thread = await db.conversation_threads.get_thread(thread_id)
    assert thread is not None
    assert thread.profile_id == "event_handler"
    assert thread.title == _dated("Garage door", mock_clock)
    # The title carries the wake's taint, as its prompt does.
    [prompt] = await db.fetch_all(
        select(message_history_table.c.taint_metadata_json).where(
            message_history_table.c.conversation_id == thread_id,
            message_history_table.c.role == "user",
        )
    )
    assert thread.title_taint is not None
    assert thread.title_taint == prompt["taint_metadata_json"]
    assert (
        TurnTaintState.from_metadata(thread.title_taint).max_tier
        != SourceTrustTier.TRUSTED_USER
    )
    assert await _profiles_of_turn_rows(db, thread_id) == {"event_handler"}


def _llm_input_text(llm_client: RuleBasedMockLLMClient) -> str:
    """Everything the model was given on its latest call, as text."""
    messages = llm_client.get_calls()[-1]["kwargs"]["messages"]
    return "\n".join(str(getattr(message, "content", message)) for message in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "web_history_budget_chars",
    [
        # As shipped: event_handler keeps no history, except what the web
        # settings it inherits allow.
        pytest.param(120_000, id="shipped"),
        pytest.param(None, id="no-history-anywhere"),
    ],
)
async def test_a_reply_in_an_event_thread_sees_the_event(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
    web_history_budget_chars: int | None,
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service(web_history_budget_chars=120_000)
    event_llm = RuleBasedMockLLMClient(
        rules=[], default_response=LLMOutput(content="The garage is open.")
    )
    event_service = _processing_service(
        profile_id="event_handler",
        web_history_budget_chars=web_history_budget_chars,
        llm_client=event_llm,
    )
    registry = {"default_assistant": service, "event_handler": event_service}
    service.processing_services_registry = registry
    event_service.processing_services_registry = registry
    chat_interface, new_task_event = await _start_worker(task_worker_manager, service)
    await execute_action(
        db_ctx=db,
        action_type=ActionType.WAKE_LLM,
        action_config={"context": "The garage door opened", "conversation": "new"},
        conversation_id=SOURCE_CONVERSATION_ID,
        interface_type="web",
        context={"trigger": "Event listener 'Garage door' matched"},
        processing_profile_id="event_handler",
        created_by_user_id=OWNER,
        tool_call_review_trigger_type="event_listener",
        tool_call_review_trigger_definition="The garage door opened",
        tool_call_review_trigger_payload_present=True,
    )
    new_task_event.set()
    await wait_for_tasks_to_complete(db_engine, task_types={"llm_callback"})
    [thread_id] = _sent_conversation_ids(chat_interface)

    # The user answers in the thread, which runs on the profile it is pinned to.
    await event_service.handle_chat_interaction(
        db_context=db,
        interface_type="web",
        conversation_id=thread_id,
        trigger_content_parts=[{"type": "text", "text": "Is it still open?"}],
        trigger_interface_message_id=None,
        user_name="Alice",
        user_id=OWNER,
        chat_interface=chat_interface,
    )

    # The recorded message list grows with the turn's own reply afterwards, so
    # the earlier reply is the one before the user's question.
    seen = _llm_input_text(event_llm)
    question = seen.index("Is it still open?")
    assert "The garage door opened" in seen[:question]
    assert "The garage is open." in seen[:question]


@pytest.mark.asyncio
def _quiet_llm(gate: asyncio.Event | None = None) -> RuleBasedMockLLMClient:
    """A model that ends every turn quietly, once ``gate`` (if any) is set."""
    return RuleBasedMockLLMClient(
        rules=[],
        default_response=LLMOutput(
            content="",
            tool_calls=[
                ToolCallItem(
                    id="call_quiet",
                    type="function",
                    function=ToolCallFunction(
                        name=END_TURN_QUIETLY_TOOL_NAME,
                        arguments='{"reason": "Nothing new today"}',
                    ),
                )
            ],
        ),
        response_gate=gate,
    )


async def _reply_in(db: Database, conversation_id: str) -> int:
    """A message the user sends into a thread, as the turn endpoint saves it."""
    return await db.message_history.add_message(
        UserMessage.from_trusted_user(content="Tell me anyway"),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=datetime.now(UTC),
        turn_id="user_turn",
        user_id=OWNER,
        processing_profile_id="default_assistant",
    )


@pytest.mark.asyncio
async def test_a_reply_sent_during_a_quiet_wake_keeps_the_thread(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    db = Database(engine=db_engine)
    gate = asyncio.Event()
    service = _processing_service(llm_client=_quiet_llm(gate))
    _, new_task_event = await _start_worker(task_worker_manager, service)

    await _wake_in_new_conversation(db)
    new_task_event.set()

    async def wake_thread() -> str | None:
        home = await find_home(
            db, service.app_config, user_id=OWNER, interface_type="web"
        )
        threads = await db.conversation_threads.list_for_home(home) if home else []
        return threads[0].conversation_id if threads else None

    # The wake's turn is held at the model while the user replies in its card.
    thread_id = await wait_for_condition(wake_thread, description="the wake thread")
    assert thread_id is not None
    reply_id = await _reply_in(db, thread_id)
    gate.set()
    await wait_for_tasks_to_complete(db_engine, task_types={"llm_callback"})

    assert await db.conversation_threads.get_thread(thread_id) is not None
    assert await db.message_history.get_row_by_internal_id(reply_id) is not None


@pytest.mark.asyncio
async def test_discarding_a_wake_thread_spares_anything_but_the_wake(
    db_engine: AsyncEngine,
) -> None:
    """What a retry of an already quiet wake relies on, as the first attempt does."""
    db = Database(engine=db_engine)
    service = _processing_service()
    now = datetime.now(UTC)
    for conversation_id in ("web_conv_replied", "web_conv_quiet"):
        await ensure_wake_thread(
            db,
            service.app_config,
            conversation_id=conversation_id,
            owner_user_id=OWNER,
            profile=service.service_config,
            title="Morning briefing · 20 Oct",
            title_taint=TurnTaintState.empty().to_metadata(),
            turn_id="wake_turn",
            now=now,
            publisher=None,
        )
        await db.message_history.add_message(
            UserMessage(
                content="System Callback Trigger: ...",
                taint_metadata=machine_authored_taint_metadata(TurnTaintState.empty()),
                authorship_taint_metadata=machine_authored_taint_metadata(
                    TurnTaintState.empty()
                ),
            ),
            interface_type="web",
            conversation_id=conversation_id,
            timestamp=now,
            turn_id="wake_turn",
            user_id=OWNER,
            processing_profile_id="default_assistant",
            is_internal=True,
        )
    reply_id = await _reply_in(db, "web_conv_replied")

    for conversation_id in ("web_conv_replied", "web_conv_quiet"):
        await discard_wake_thread(
            db,
            conversation_id=conversation_id,
            turn_id="wake_turn",
            owner_user_id=OWNER,
            publisher=None,
        )

    assert await db.conversation_threads.get_thread("web_conv_replied") is not None
    # The reply keeps the whole thread, the wake prompt it answers included.
    kept = await db.fetch_all(
        select(message_history_table.c.internal_id).where(
            message_history_table.c.conversation_id == "web_conv_replied"
        )
    )
    assert len(kept) == 2
    assert await db.message_history.get_row_by_internal_id(reply_id) is not None
    assert await db.conversation_threads.get_thread("web_conv_quiet") is None
    assert (
        await db.fetch_all(
            select(message_history_table.c.internal_id).where(
                message_history_table.c.conversation_id == "web_conv_quiet"
            )
        )
        == []
    )


@pytest.mark.asyncio
async def test_a_wake_that_ends_quietly_leaves_no_thread(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service(llm_client=_quiet_llm())
    chat_interface, new_task_event = await _start_worker(task_worker_manager, service)

    await _wake_in_new_conversation(db)
    new_task_event.set()
    await wait_for_tasks_to_complete(db_engine, task_types={"llm_callback"})

    assert _sent_conversation_ids(chat_interface) == []
    home = await _home(db, service)
    assert await db.conversation_threads.list_for_home(home) == []
    assert (
        await list_home_threads(
            db, home, turn_running=lambda _conversation_id: False, viewer=None
        )
        == []
    )
    leftover = await db.fetch_all(
        select(message_history_table.c.internal_id).where(
            message_history_table.c.conversation_id != home
        )
    )
    assert leftover == []


class _RecordingNotifier:
    def __init__(self) -> None:
        self.enabled = True
        self.calls: list[tuple[str, str | None]] = []

    async def send_notification(
        self,
        user_identifier: str,
        title: str,
        body: str,
        db_context: Database,
        *,
        metadata: NotificationMetadata | None = None,
    ) -> None:
        self.calls.append((
            user_identifier,
            metadata.conversation_id if metadata else None,
        ))


@pytest.mark.asyncio
async def test_push_for_a_wake_thread_targets_the_thread(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service()
    notifier = _RecordingNotifier()
    _, new_task_event = await _start_worker(
        task_worker_manager, service, WebChatInterface(db_engine, notifier=notifier)
    )

    await _wake_in_new_conversation(db)
    new_task_event.set()
    await wait_for_tasks_to_complete(db_engine, task_types={"llm_callback"})

    [thread] = await db.conversation_threads.list_for_home(await _home(db, service))
    assert notifier.calls == [(OWNER, thread.conversation_id)]


@pytest.mark.asyncio
async def test_without_threads_a_new_conversation_wake_starts_no_thread(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service(threads=False)
    chat_interface, new_task_event = await _start_worker(task_worker_manager, service)

    await _wake_in_new_conversation(db)
    new_task_event.set()
    await wait_for_tasks_to_complete(db_engine, task_types={"llm_callback"})

    [sent_to] = _sent_conversation_ids(chat_interface)
    assert sent_to.startswith("web_conv_")
    assert sent_to != SOURCE_CONVERSATION_ID
    assert await db.conversation_threads.get_thread(sent_to) is None
    assert (
        await find_home(db, service.app_config, user_id=OWNER, interface_type="web")
        is None
    )


@pytest.mark.asyncio
async def test_a_telegram_wake_still_cannot_open_a_new_conversation(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service()
    chat_interface, new_task_event = await _start_worker(task_worker_manager, service)

    await execute_action(
        db_ctx=db,
        action_type=ActionType.WAKE_LLM,
        action_config={"context": "Send the morning briefing", "conversation": "new"},
        conversation_id="123456",
        interface_type="telegram",
        created_by_user_id=OWNER,
    )
    new_task_event.set()
    await wait_for_tasks_to_complete(
        db_engine, task_types={"llm_callback"}, allow_failures=True
    )

    assert _sent_conversation_ids(chat_interface) == []
    statuses = [
        row["status"] for row in await db.fetch_all(select(tasks_table.c.status))
    ]
    assert statuses == ["failed"]
    assert (
        await find_home(db, service.app_config, user_id=OWNER, interface_type="web")
        is None
    )


@pytest.mark.asyncio
async def test_ensuring_a_wake_thread_twice_records_it_once(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    service = _processing_service()
    now = datetime.now(UTC)

    for _attempt in range(2):
        await ensure_wake_thread(
            db,
            service.app_config,
            conversation_id="web_conv_wake",
            owner_user_id=OWNER,
            profile=service.service_config,
            title="Morning briefing · 20 Oct",
            title_taint=TurnTaintState.empty().to_metadata(),
            turn_id="wake_turn",
            now=now,
            publisher=None,
        )

    threads = await db.conversation_threads.list_for_home(await _home(db, service))
    assert [thread.conversation_id for thread in threads] == ["web_conv_wake"]
