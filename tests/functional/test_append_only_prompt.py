"""Each request is the previous one plus appended messages.

The property the prompt layout exists for (docs/design/append-only-prompt.md):
a provider's prefix cache, and Anthropic's binding of thinking blocks to the
history before them, both depend on nothing earlier in the prompt changing
between requests.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.context_providers import KnownUsersContextProvider
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput, ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import ToolMessage
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.processing.household_context import HOUSEHOLD_CONTEXT_HEADING
from family_assistant.storage.database import Database
from family_assistant.tools.infrastructure import (
    LocalToolsProvider,
    TaintTrackingToolsProvider,
)
from family_assistant.tools.metadata import (
    ToolImplementation,
    ToolRegistration,
    ToolTag,
    make_local_tool_metadata,
)
from tests.mocks.mock_llm import RuleBasedMockLLMClient

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.llm.messages import LLMMessage
    from family_assistant.tools.types import ToolDefinition
    from family_assistant.utils.clock import MockClock
    from tests.mocks.mock_llm import MatcherArgs


@pytest.mark.asyncio
async def test_the_next_turn_extends_the_previous_request(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    requests: list[list[LLMMessage]] = []

    def record(args: MatcherArgs) -> bool:
        # Copied: the loop keeps appending to the list it passed.
        requests.append(list(args["messages"]))
        return True

    service = ProcessingService(
        llm_client=RuleBasedMockLLMClient(
            rules=[(record, LLMOutput(content="Noted.", tool_calls=None))]
        ),
        tools_provider=LocalToolsProvider(registrations=[]),
        service_config=ProcessingServiceConfig(
            prompts={"system_prompt": "You are a test assistant."},
            timezone=ZoneInfo("Australia/Sydney"),
            history_budget_chars=100_000,
            history_max_age_hours=24,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
            id="append-only",
            include_aggregated_context=True,
        ),
        context_providers=[
            KnownUsersContextProvider(chat_id_to_name_map={123: "Alice"}, prompts={})
        ],
        server_url=None,
        app_config=AppConfig(),
        clock=mock_clock,
    )
    db = Database(db_engine)
    for text in ("First question", "Second question"):
        result = await service.handle_chat_interaction(
            db_context=db,
            interface_type="web",
            conversation_id="append-only-conversation",
            trigger_content_parts=[{"type": "text", "text": text}],
            trigger_interface_message_id=None,
            user_name="Alice",
        )
        assert result.status.value == "success"
        mock_clock.advance(timedelta(minutes=5))

    assert len(requests) == 2
    first, second = requests[0], requests[1]
    assert second[: len(first)] == first
    system_prompt = first[0].content
    assert isinstance(system_prompt, str)
    assert HOUSEHOLD_CONTEXT_HEADING in system_prompt
    assert "Alice (Chat ID: 123)" in system_prompt
    first_question = first[1].content
    assert isinstance(first_question, str)
    assert first_question.startswith("[Sent ")
    assert second[-1].content != first_question


def _registration(name: str, implementation: ToolImplementation) -> ToolRegistration:
    return ToolRegistration(
        definition=cast(
            "ToolDefinition",
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"The {name} tool.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ),
        implementation=implementation,
        metadata=make_local_tool_metadata([
            ToolTag.READ_ONLY,
            ToolTag.OUTPUT_TRUSTED,
        ]),
    )


def _without_taint(messages: list[LLMMessage]) -> list[LLMMessage]:
    return [message.model_copy(update={"taint_metadata": None}) for message in messages]


@pytest.mark.asyncio
async def test_parallel_results_replay_in_the_order_they_were_sent(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    """Parallel tool results that finish out of order still extend the prompt.

    History replay emits each result directly after its call, in call order, so
    the live request has to use that order too, whatever order the tools
    finished in.
    """
    fast_finished = asyncio.Event()

    async def slow_tool(**_kwargs: object) -> str:
        await fast_finished.wait()
        return "slow result"

    async def fast_tool(**_kwargs: object) -> str:
        fast_finished.set()
        return "fast result"

    requests: list[list[LLMMessage]] = []

    def record(args: MatcherArgs) -> bool:
        requests.append(list(args["messages"]))
        return False

    parallel_calls = LLMOutput(
        content="",
        tool_calls=[
            ToolCallItem(
                id="call_slow",
                type="function",
                function=ToolCallFunction(name="slow_tool", arguments="{}"),
            ),
            ToolCallItem(
                id="call_fast",
                type="function",
                function=ToolCallFunction(name="fast_tool", arguments="{}"),
            ),
        ],
    )
    service = ProcessingService(
        llm_client=RuleBasedMockLLMClient(
            rules=[
                (record, LLMOutput(content="unreachable")),
                (lambda _args: len(requests) == 1, parallel_calls),
                (lambda _args: True, LLMOutput(content="Done.")),
            ]
        ),
        tools_provider=TaintTrackingToolsProvider(
            LocalToolsProvider(
                registrations=[
                    _registration("slow_tool", slow_tool),
                    _registration("fast_tool", fast_tool),
                ]
            )
        ),
        service_config=ProcessingServiceConfig(
            prompts={"system_prompt": "You are a test assistant."},
            timezone=ZoneInfo("Australia/Sydney"),
            history_budget_chars=100_000,
            history_max_age_hours=24,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
            id="append-only",
        ),
        context_providers=[],
        server_url=None,
        app_config=AppConfig(),
        clock=mock_clock,
    )
    db = Database(db_engine)
    for text in ("Run both tools", "And then?"):
        result = await service.handle_chat_interaction(
            db_context=db,
            interface_type="web",
            conversation_id="parallel-order-conversation",
            trigger_content_parts=[{"type": "text", "text": text}],
            trigger_interface_message_id=None,
            user_name="Alice",
        )
        assert result.status.value == "success"
        mock_clock.advance(timedelta(minutes=5))

    assert len(requests) == 3
    after_tools, next_turn = requests[1], requests[2]
    assert [
        message.tool_call_id
        for message in after_tools
        if isinstance(message, ToolMessage)
    ] == ["call_slow", "call_fast"]
    # Taint metadata is the app's own bookkeeping, re-derived when history is
    # loaded, and no provider adapter sends it.
    assert _without_taint(next_turn[: len(after_tools)]) == _without_taint(after_tools)
