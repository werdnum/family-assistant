"""A wake can open a fresh web conversation instead of waking its source one."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from family_assistant.actions import (
    ActionType,
    NewConversationError,
    execute_action,
    validate_wake_destination,
)
from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.interfaces import ChatInterface
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.database import Database
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.tasks import tasks_table
from family_assistant.task_worker import (
    TaskWorker,
    handle_llm_callback,
    handle_script_execution,
)
from family_assistant.tools import CompositeToolsProvider, LocalToolsProvider
from tests.helpers import wait_for_tasks_to_complete
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncEngine

SOURCE_CONVERSATION_ID = "web_conv_source"
OWNER = "alice@example.com"


async def _start_worker(
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> tuple[AsyncMock, asyncio.Event]:
    tools_provider = CompositeToolsProvider(
        providers=[LocalToolsProvider(definitions=[], implementations={})]
    )
    await tools_provider.get_tool_definitions()
    processing_service = ProcessingService(
        llm_client=RuleBasedMockLLMClient(
            rules=[], default_response=LLMOutput(content="Here is your briefing.")
        ),
        tools_provider=tools_provider,
        service_config=ProcessingServiceConfig(
            id="default_assistant",
            prompts={"system_prompt": "Assistant"},
            timezone=ZoneInfo("UTC"),
            history_budget_chars=0,
            history_min_turns=0,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.BLOCKED,
        ),
        app_config=AppConfig(),
        context_providers=[],
        server_url=None,
    )
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "sent_message_id"
    task_worker, new_task_event, _ = task_worker_manager(
        processing_service=processing_service,
        chat_interface=chat_interface,
        register_delegation_handler=False,
    )
    task_worker.register_task_handler("llm_callback", handle_llm_callback)
    task_worker.register_task_handler("script_execution", handle_script_execution)
    return chat_interface, new_task_event


async def _wake(
    db: Database,
    *,
    conversation: str = "new",
    interface_type: str = "web",
    owner: str | None = OWNER,
) -> None:
    await execute_action(
        db_ctx=db,
        action_type=ActionType.WAKE_LLM,
        action_config={
            "context": "Send the morning briefing",
            "conversation": conversation,
        },
        conversation_id=SOURCE_CONVERSATION_ID,
        interface_type=interface_type,
        created_by_user_id=owner,
    )


def _sent_conversation_ids(chat_interface: AsyncMock) -> list[str]:
    return [
        call.kwargs["conversation_id"]
        for call in chat_interface.send_message.call_args_list
    ]


@pytest.mark.asyncio
async def test_new_conversation_wake_replies_in_a_fresh_conversation(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    db = Database(engine=db_engine)
    chat_interface, new_task_event = await _start_worker(task_worker_manager)

    await _wake(db)
    new_task_event.set()
    await wait_for_tasks_to_complete(db_engine, task_types={"llm_callback"})

    [sent_to] = _sent_conversation_ids(chat_interface)
    assert sent_to.startswith("web_conv_")
    assert sent_to != SOURCE_CONVERSATION_ID


@pytest.mark.asyncio
async def test_new_conversation_is_listed_for_its_owner_only(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    db = Database(engine=db_engine)
    chat_interface, new_task_event = await _start_worker(task_worker_manager)

    await _wake(db)
    new_task_event.set()
    await wait_for_tasks_to_complete(db_engine, task_types={"llm_callback"})

    [new_conversation_id] = _sent_conversation_ids(chat_interface)
    owner_summaries, _ = await db.message_history.get_conversation_summaries(
        interface_type="web", owner_user_ids={OWNER}
    )
    other_summaries, _ = await db.message_history.get_conversation_summaries(
        interface_type="web", owner_user_ids={"bob@example.com"}
    )
    assert new_conversation_id in {s["conversation_id"] for s in owner_summaries}
    assert new_conversation_id not in {s["conversation_id"] for s in other_summaries}


@pytest.mark.asyncio
async def test_each_firing_opens_its_own_conversation(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    db = Database(engine=db_engine)
    chat_interface, new_task_event = await _start_worker(task_worker_manager)

    await _wake(db)
    await _wake(db)
    new_task_event.set()
    await wait_for_tasks_to_complete(db_engine, task_types={"llm_callback"})

    sent_to = _sent_conversation_ids(chat_interface)
    assert len(set(sent_to)) == 2


@pytest.mark.asyncio
async def test_new_conversation_wake_leaves_source_conversation_untouched(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    db = Database(engine=db_engine)
    _, new_task_event = await _start_worker(task_worker_manager)

    await _wake(db)
    new_task_event.set()
    await wait_for_tasks_to_complete(db_engine, task_types={"llm_callback"})

    rows = await db.fetch_all(
        select(message_history_table.c.internal_id).where(
            message_history_table.c.conversation_id == SOURCE_CONVERSATION_ID
        )
    )
    assert rows == []


@pytest.mark.asyncio
async def test_new_conversation_wake_from_telegram_fails_without_sending(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    db = Database(engine=db_engine)
    chat_interface, new_task_event = await _start_worker(task_worker_manager)

    await _wake(db, interface_type="telegram")
    new_task_event.set()
    await wait_for_tasks_to_complete(
        db_engine, task_types={"llm_callback"}, allow_failures=True
    )

    rows = await db.fetch_all(
        select(tasks_table.c.status).where(tasks_table.c.task_type == "llm_callback")
    )
    assert [row["status"] for row in rows] == ["failed"]
    chat_interface.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_script_wakes_split_by_destination(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    db = Database(engine=db_engine)
    chat_interface, new_task_event = await _start_worker(task_worker_manager)

    await execute_action(
        db_ctx=db,
        action_type=ActionType.SCRIPT,
        action_config={
            "script_code": (
                'wake_llm("keep talking here")\n'
                'wake_llm("start fresh", new_conversation=True)\n'
            )
        },
        conversation_id=SOURCE_CONVERSATION_ID,
        interface_type="web",
        created_by_user_id=OWNER,
    )
    new_task_event.set()
    await wait_for_tasks_to_complete(
        db_engine, task_types={"script_execution", "llm_callback"}
    )

    sent_to = _sent_conversation_ids(chat_interface)
    assert SOURCE_CONVERSATION_ID in sent_to
    assert len(set(sent_to)) == 2


@pytest.mark.parametrize(
    ("action_config", "interface_type", "owner"),
    [
        pytest.param({"conversation": "elsewhere"}, "web", OWNER, id="unknown-value"),
        pytest.param({"conversation": "new"}, "telegram", OWNER, id="telegram"),
        pytest.param({"conversation": "new"}, "web", None, id="no-owner"),
    ],
)
def test_validate_wake_destination_refuses(
    action_config: dict[str, str], interface_type: str, owner: str | None
) -> None:
    with pytest.raises(NewConversationError):
        validate_wake_destination(
            "wake_llm",
            action_config,
            interface_type=interface_type,
            owner_user_id=owner,
        )


def test_validate_wake_destination_ignores_scripts() -> None:
    validate_wake_destination(
        "script",
        {"conversation": "new"},
        interface_type="telegram",
        owner_user_id=None,
    )
