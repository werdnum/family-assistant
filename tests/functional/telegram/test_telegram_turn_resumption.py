"""Telegram turns survive the process that ran them going away.

Covers Milestone 2 of docs/design/turn-resumption-across-restarts.md: a Telegram
turn saves its rows as it goes and holds a lease while it runs, a graceful
shutdown suspends it without replying, and a due lease relaunches it -- or, for
a turn whose reply was generated but never sent, delivers that reply.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from telegram import Update

from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import AssistantMessage, ToolMessage, UserMessage
from family_assistant.services.turn_resumption import (
    TURN_RESUME_TASK_TYPE,
    TurnLeaseRegistry,
    TurnResumePayload,
)
from family_assistant.storage.database import Database
from family_assistant.telegram.turn_resumption import TELEGRAM_RESUMER
from family_assistant.tools.types import ToolExecutionContext
from tests.helpers import wait_for_condition
from tests.mocks.mock_llm import LLMOutput, MatcherArgs, RuleBasedMockLLMClient

from .conftest import TelegramHandlerTestFixture
from .helpers import assert_bot_sent_message
from .test_telegram_handler import create_mock_context

CHAT_ID = "123"
TELEGRAM_USER_ID = 12345
RESUMED_REPLY = "Here is what I found after the restart"


def _note_tool_call(call_id: str = "call_note") -> ToolCallItem:
    return ToolCallItem(
        id=call_id,
        type="function",
        function=ToolCallFunction(
            name="add_or_update_note",
            arguments=json.dumps({"title": "Resumed", "content": "original"}),
        ),
    )


def _registry(fix: TelegramHandlerTestFixture) -> TurnLeaseRegistry:
    app = fix.assistant.fastapi_app
    assert app is not None
    return app.state.turn_lease_registry


def _user_id(fix: TelegramHandlerTestFixture) -> str:
    return fix.handler.user_identity_resolver.resolve_telegram_user(
        TELEGRAM_USER_ID
    ).user_id


async def _pending_leases(db: Database) -> list[TurnResumePayload]:
    rows = await db.tasks.get_all(task_type=TURN_RESUME_TASK_TYPE, status="pending")
    return [
        payload
        for payload in (
            TurnResumePayload.model_validate(row["payload"]) for row in rows
        )
        if payload.conversation_id == CHAT_ID
    ]


async def _turn_rows(db: Database) -> list[str]:
    """Roles of the chat's rows, oldest first."""
    rows = await db.message_history.get_recent_with_metadata(
        interface_type="telegram", conversation_id=CHAT_ID, limit=50
    )
    return [row["role"] for row in rows]


class _SlowTool:
    """Stands in for the tools provider's execute_tool, parked until released."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - tool arguments are arbitrary JSON
        arguments: dict[str, Any],
        context: Any,  # noqa: ANN401
        call_id: str | None = None,
    ) -> str:
        self.started.set()
        await self.release.wait()
        return "tool completed"


async def _start_turn_with_slow_tool(
    fix: TelegramHandlerTestFixture, slow_tool: _SlowTool
) -> "asyncio.Task[None]":
    mock_llm = cast("RuleBasedMockLLMClient", fix.mock_llm)
    mock_llm.rules = [
        (
            lambda kwargs: not any(m.role == "tool" for m in kwargs["messages"]),
            LLMOutput(tool_calls=[_note_tool_call()]),
        ),
        (lambda kwargs: True, LLMOutput(content="All done.")),
    ]
    fix.tools_provider.execute_tool = slow_tool  # type: ignore[method-assign]
    sent = await fix.telegram_client.send_message("Start the note")
    update = Update.de_json(sent.get("result", {}), fix.bot)
    task = asyncio.create_task(
        fix.handler.message_handler(update, create_mock_context(fix.application))
    )
    await wait_for_condition(
        slow_tool.started.is_set, timeout=10, description="slow tool to start"
    )
    return task


def _exec_context(db: Database) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="unknown",
        conversation_id="unknown",
        user_name="task_worker",
        turn_id=None,
        db_context=db,
        processing_service=None,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


async def _seed_turn(
    fix: TelegramHandlerTestFixture,
    turn_id: str,
    messages: list[UserMessage | AssistantMessage | ToolMessage],
) -> None:
    """Persist a turn as an interrupted run would have left it.

    The prompt is a real message on the test server, so the reply can be
    threaded to it.
    """
    sent = await fix.telegram_client.send_message("What's on my notes list?")
    update = Update.de_json(sent.get("result", {}), fix.bot)
    assert update is not None
    assert update.message is not None
    prompt_message_id = str(update.message.message_id)
    profile_id = fix.processing_service.service_config.id
    for index, message in enumerate(messages):
        await fix.database.message_history.add_message(
            message,
            interface_type="telegram",
            conversation_id=CHAT_ID,
            interface_message_id=prompt_message_id if index == 0 else None,
            turn_id=turn_id,
            timestamp=datetime.now(UTC),
            user_id=_user_id(fix),
            processing_profile_id=profile_id,
        )


async def _resume(fix: TelegramHandlerTestFixture, turn_id: str) -> None:
    payload = TurnResumePayload(
        resumer=TELEGRAM_RESUMER,
        interface_type="telegram",
        conversation_id=CHAT_ID,
        turn_id=turn_id,
        user_id=_user_id(fix),
        user_name="TestUser",
        processing_profile_id=fix.processing_service.service_config.id,
    )
    await _registry(fix).handle_resume_task(
        _exec_context(fix.database), payload.model_dump(mode="json")
    )


@pytest.mark.asyncio
async def test_telegram_turn_saves_its_tool_round_while_the_tool_runs(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """The tool-calling row is durable before the turn finishes, so a turn
    killed mid-tool leaves something to resume from."""
    fix = telegram_handler_fixture
    slow_tool = _SlowTool()
    original_execute_tool = fix.tools_provider.execute_tool
    task = await _start_turn_with_slow_tool(fix, slow_tool)
    try:
        roles = await _turn_rows(fix.database)
    finally:
        slow_tool.release.set()
        await asyncio.wait_for(task, timeout=10)
        fix.tools_provider.execute_tool = original_execute_tool  # type: ignore[method-assign]

    assert roles == ["user", "assistant"]


@pytest.mark.asyncio
async def test_running_telegram_turn_holds_a_lease(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    fix = telegram_handler_fixture
    slow_tool = _SlowTool()
    original_execute_tool = fix.tools_provider.execute_tool
    task = await _start_turn_with_slow_tool(fix, slow_tool)
    try:
        leases = await _pending_leases(fix.database)
    finally:
        slow_tool.release.set()
        await asyncio.wait_for(task, timeout=10)
        fix.tools_provider.execute_tool = original_execute_tool  # type: ignore[method-assign]

    assert [(lease.resumer, lease.attempt) for lease in leases] == [
        (TELEGRAM_RESUMER, 0)
    ]


@pytest.mark.asyncio
async def test_telegram_turn_releases_its_lease_once_it_replies(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    fix = telegram_handler_fixture
    mock_llm = cast("RuleBasedMockLLMClient", fix.mock_llm)
    mock_llm.rules = [(lambda kwargs: True, LLMOutput(content="Quick answer"))]
    sent = await fix.telegram_client.send_message("Quick question")
    update = Update.de_json(sent.get("result", {}), fix.bot)

    await fix.handler.message_handler(update, create_mock_context(fix.application))

    await assert_bot_sent_message(fix.telegram_client, "Quick answer", timeout=10)

    async def lease_released() -> bool:
        return not await _pending_leases(fix.database)

    await wait_for_condition(lease_released, timeout=10, description="lease released")


@pytest.mark.asyncio
async def test_suspended_telegram_turn_keeps_its_rows_and_its_lease(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """A shutdown lets the running tool finish and record its result, then
    stops the turn before it replies, leaving the lease for the next process."""
    fix = telegram_handler_fixture
    slow_tool = _SlowTool()
    original_execute_tool = fix.tools_provider.execute_tool
    task = await _start_turn_with_slow_tool(fix, slow_tool)
    try:
        suspension = asyncio.create_task(_registry(fix).suspend_all(grace_seconds=30.0))
        slow_tool.release.set()
        await asyncio.wait_for(suspension, timeout=20)
        await asyncio.wait_for(task, timeout=10)
    finally:
        slow_tool.release.set()
        fix.tools_provider.execute_tool = original_execute_tool  # type: ignore[method-assign]

    assert await _turn_rows(fix.database) == ["user", "assistant", "tool"]
    assert len(await _pending_leases(fix.database)) == 1


@pytest.mark.asyncio
async def test_resumed_telegram_turn_replies_in_the_chat(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    fix = telegram_handler_fixture
    mock_llm = cast("RuleBasedMockLLMClient", fix.mock_llm)

    def after_tool_result(kwargs: MatcherArgs) -> bool:
        return any(message.role == "tool" for message in kwargs["messages"])

    mock_llm.rules = [(after_tool_result, LLMOutput(content=RESUMED_REPLY))]
    turn_id = str(uuid.uuid4())
    await _seed_turn(
        fix,
        turn_id,
        [
            UserMessage.from_trusted_user(content="What's on my notes list?"),
            AssistantMessage(content="", tool_calls=[_note_tool_call("call_1")]),
            ToolMessage(
                tool_call_id="call_1", name="add_or_update_note", content="Saved."
            ),
        ],
    )

    await _resume(fix, turn_id)

    await assert_bot_sent_message(fix.telegram_client, RESUMED_REPLY, timeout=10)


@pytest.mark.asyncio
async def test_finished_but_undelivered_telegram_reply_is_sent(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """A turn whose process stopped between saving its reply and sending it
    gets that reply delivered, without running the turn again."""
    fix = telegram_handler_fixture
    turn_id = str(uuid.uuid4())
    await _seed_turn(
        fix,
        turn_id,
        [
            UserMessage.from_trusted_user(content="What's on my notes list?"),
            AssistantMessage(content=RESUMED_REPLY),
        ],
    )

    await _resume(fix, turn_id)

    await assert_bot_sent_message(fix.telegram_client, RESUMED_REPLY, timeout=10)
    assert (
        await fix.database.message_history.get_undelivered_terminal_reply(turn_id)
        is None
    )
