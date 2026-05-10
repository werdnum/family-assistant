import asyncio
import json
from typing import Any, cast

import pytest
from telegram import Update

from family_assistant.llm import ToolCallFunction, ToolCallItem
from tests.helpers import wait_for_condition
from tests.mocks.mock_llm import LLMOutput, MatcherArgs, RuleBasedMockLLMClient

from .conftest import TelegramHandlerTestFixture
from .helpers import assert_bot_sent_message
from .test_telegram_handler import create_mock_context


def _tool_call() -> ToolCallItem:
    return ToolCallItem(
        id="call_note",
        type="function",
        function=ToolCallFunction(
            name="add_or_update_note",
            arguments=json.dumps({"title": "Mid turn", "content": "original"}),
        ),
    )


@pytest.mark.asyncio
async def test_follow_up_message_steers_active_tool_turn(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    fix = telegram_handler_fixture
    mock_llm = cast("RuleBasedMockLLMClient", fix.mock_llm)
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()

    async def slow_execute_tool(
        name: str,
        # ast-grep-ignore: no-dict-any - tool arguments are arbitrary JSON
        arguments: dict[str, Any],
        context: Any,  # noqa: ANN401
        call_id: str | None = None,
    ) -> str:
        tool_started.set()
        await release_tool.wait()
        return "tool completed"

    original_execute_tool = fix.tools_provider.execute_tool
    fix.tools_provider.execute_tool = slow_execute_tool  # type: ignore[method-assign]

    def first_call(kwargs: MatcherArgs) -> bool:
        return not any(msg.role == "tool" for msg in kwargs["messages"])

    def second_call(kwargs: MatcherArgs) -> bool:
        messages = kwargs["messages"]
        return (
            len(messages) >= 3
            and messages[-3].role == "assistant"
            and messages[-2].role == "tool"
            and messages[-1].role == "user"
            and "Actually make it urgent" in str(messages[-1].content)
        )

    mock_llm.rules = [
        (first_call, LLMOutput(tool_calls=[_tool_call()])),
        (second_call, LLMOutput(content="I made it urgent.")),
    ]

    try:
        first_result = await fix.telegram_client.send_message("Start the note")
        first_update = Update.de_json(first_result.get("result", {}), fix.bot)
        context = create_mock_context(fix.application)
        active_task = asyncio.create_task(
            fix.handler.message_handler(first_update, context)
        )

        await wait_for_condition(
            tool_started.is_set,
            timeout=5,
            description="slow tool execution to start",
        )

        follow_up_result = await fix.telegram_client.send_message(
            "Actually make it urgent"
        )
        follow_up_update = Update.de_json(follow_up_result.get("result", {}), fix.bot)
        await fix.handler.message_handler(follow_up_update, context)

        release_tool.set()
        await asyncio.wait_for(active_task, timeout=10)

        await assert_bot_sent_message(
            fix.telegram_client,
            "I'll apply that to the current response.",
            timeout=5,
        )
        await assert_bot_sent_message(
            fix.telegram_client,
            "I made it urgent",
            timeout=5,
        )
    finally:
        release_tool.set()
        fix.tools_provider.execute_tool = original_execute_tool  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_interrupt_cancels_active_telegram_turn(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    fix = telegram_handler_fixture
    mock_llm = cast("RuleBasedMockLLMClient", fix.mock_llm)
    tool_started = asyncio.Event()

    async def slow_execute_tool(
        name: str,
        # ast-grep-ignore: no-dict-any - tool arguments are arbitrary JSON
        arguments: dict[str, Any],
        context: Any,  # noqa: ANN401
        call_id: str | None = None,
    ) -> str:
        tool_started.set()
        await asyncio.Event().wait()
        return "should not complete"

    original_execute_tool = fix.tools_provider.execute_tool
    fix.tools_provider.execute_tool = slow_execute_tool  # type: ignore[method-assign]
    mock_llm.rules = [(lambda kwargs: True, LLMOutput(tool_calls=[_tool_call()]))]

    try:
        first_result = await fix.telegram_client.send_message("Start long task")
        first_update = Update.de_json(first_result.get("result", {}), fix.bot)
        context = create_mock_context(fix.application)
        active_task = asyncio.create_task(
            fix.handler.message_handler(first_update, context)
        )

        await wait_for_condition(
            tool_started.is_set,
            timeout=5,
            description="slow tool execution to start",
        )

        interrupt_result = await fix.telegram_client.send_command("/interrupt")
        interrupt_update = Update.de_json(interrupt_result.get("result", {}), fix.bot)
        await fix.handler.interrupt_command(interrupt_update, context)
        await asyncio.wait_for(active_task, timeout=10)

        await assert_bot_sent_message(
            fix.telegram_client,
            "Interrupted the current request.",
            timeout=5,
        )
    finally:
        fix.tools_provider.execute_tool = original_execute_tool  # type: ignore[method-assign]
