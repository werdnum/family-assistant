"""Each request is the previous one plus appended messages.

The property the prompt layout exists for (docs/design/append-only-prompt.md):
a provider's prefix cache, and Anthropic's binding of thinking blocks to the
history before them, both depend on nothing earlier in the prompt changing
between requests.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.context_providers import KnownUsersContextProvider
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.processing.household_context import HOUSEHOLD_CONTEXT_HEADING
from family_assistant.storage.database import Database
from family_assistant.tools.infrastructure import LocalToolsProvider
from tests.mocks.mock_llm import RuleBasedMockLLMClient

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.llm.messages import LLMMessage
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
            max_history_messages=20,
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
