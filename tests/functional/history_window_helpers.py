"""Shared setup for the history window and compaction functional tests."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput
from family_assistant.llm.messages import (
    AssistantMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.security.taint import TurnTaintState
from family_assistant.tools.infrastructure import LocalToolsProvider
from tests.mocks.mock_llm import RuleBasedMockLLMClient

if TYPE_CHECKING:
    from collections.abc import Sequence

    from family_assistant.llm import LLMInterface
    from family_assistant.llm.messages import LLMMessage
    from family_assistant.storage.database import Database
    from family_assistant.tools.metadata import ToolRegistration
    from family_assistant.utils.clock import MockClock
    from tests.mocks.mock_llm import MatcherArgs

PROFILE_ID = "history-window"
CONVERSATION_ID = "history-window-conversation"


def user_message(content: str) -> UserMessage:
    empty = TurnTaintState.empty().to_metadata()
    return UserMessage(
        content=content, taint_metadata=empty, authorship_taint_metadata=empty
    )


class Recorder:
    def __init__(self) -> None:
        self.requests: list[list[LLMMessage]] = []

    def __call__(self, args: MatcherArgs) -> bool:
        self.requests.append(list(args["messages"]))
        return True


def make_service(
    mock_clock: MockClock,
    recorder: Recorder,
    *,
    budget_chars: int,
    min_turns: int = 1,
    max_age_hours: float = 24,
    idle_gap_minutes: float | None = None,
    profile_id: str = PROFILE_ID,
    tools: Sequence[ToolRegistration] = (),
    llm_client: LLMInterface | None = None,
) -> ProcessingService:
    return ProcessingService(
        llm_client=llm_client
        or RuleBasedMockLLMClient(
            rules=[(recorder, LLMOutput(content="Noted.", tool_calls=None))]
        ),
        tools_provider=LocalToolsProvider(registrations=list(tools)),
        service_config=ProcessingServiceConfig(
            prompts={"system_prompt": "You are a test assistant."},
            timezone=ZoneInfo("UTC"),
            history_budget_chars=budget_chars,
            history_min_turns=min_turns,
            history_max_age_hours=max_age_hours,
            history_idle_gap_minutes=idle_gap_minutes,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
            id=profile_id,
        ),
        context_providers=[],
        server_url=None,
        app_config=AppConfig(),
        clock=mock_clock,
    )


async def seed_turn(
    db: Database,
    mock_clock: MockClock,
    request: str,
    answer: str,
    *,
    tool_calls: int = 0,
    tool_result: str = "result",
    interface_type: str = "telegram",
    profile_id: str = PROFILE_ID,
    thread_root_id: int | None = None,
    interface_message_id: str | None = None,
) -> int:
    """Store one completed turn; returns its user row's internal id."""
    turn_id = str(uuid.uuid4())
    common = {
        "interface_type": interface_type,
        "conversation_id": CONVERSATION_ID,
        "turn_id": turn_id,
        "processing_profile_id": profile_id,
        "thread_root_id": thread_root_id,
    }
    user_row = await db.message_history.add_message(
        user_message(request),
        timestamp=mock_clock.now(),
        interface_message_id=interface_message_id,
        **common,
    )
    for index in range(tool_calls):
        mock_clock.advance(timedelta(seconds=1))
        call_id = f"{turn_id}-{index}"
        await db.message_history.add_message(
            AssistantMessage(
                content=None,
                tool_calls=[
                    ToolCallItem(
                        id=call_id,
                        type="function",
                        function=ToolCallFunction(name="lookup", arguments="{}"),
                    )
                ],
            ),
            timestamp=mock_clock.now(),
            **common,
        )
        await db.message_history.add_message(
            ToolMessage(tool_call_id=call_id, name="lookup", content=tool_result),
            timestamp=mock_clock.now(),
            **common,
        )
    mock_clock.advance(timedelta(seconds=1))
    await db.message_history.add_message(
        AssistantMessage(content=answer), timestamp=mock_clock.now(), **common
    )
    mock_clock.advance(timedelta(minutes=1))
    return user_row


async def run_turn(
    service: ProcessingService,
    db: Database,
    text: str,
    *,
    interface_type: str = "telegram",
    replied_to_interface_id: str | None = None,
) -> None:
    result = await service.handle_chat_interaction(
        db_context=db,
        interface_type=interface_type,
        conversation_id=CONVERSATION_ID,
        trigger_content_parts=[{"type": "text", "text": text}],
        trigger_interface_message_id=None,
        user_name="Alice",
        replied_to_interface_id=replied_to_interface_id,
    )
    assert result.status.value == "success"


def texts(messages: list[LLMMessage]) -> str:
    return "\n".join(
        message.content
        for message in messages
        if isinstance(message, UserMessage | AssistantMessage | ToolMessage)
        and isinstance(message.content, str)
    )
