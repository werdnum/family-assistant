import json
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import AssistantMessage, ToolMessage, UserMessage
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.processing.types import MidTurnUserInput
from family_assistant.storage.context import get_db_context
from family_assistant.tools.types import ToolDefinition, ToolExecutionContext
from tests.mocks.mock_llm import LLMOutput, MatcherArgs, RuleBasedMockLLMClient


class MidTurnProvider:
    def __init__(self, inputs: list[MidTurnUserInput]) -> None:
        self._inputs = inputs

    async def drain_pending_mid_turn_inputs(self) -> list[MidTurnUserInput]:
        inputs = self._inputs
        self._inputs = []
        return inputs

    def should_interrupt(self) -> bool:
        return False


class ToolProvider:
    async def get_tool_definitions(
        self,
    ) -> list[ToolDefinition]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "Lookup test data",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            }
        ]

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - tool arguments are arbitrary JSON
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str:
        return "tool result"

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_mid_turn_input_is_injected_after_tool_result(
    db_engine: Any,  # noqa: ANN401
) -> None:
    tool_call = ToolCallItem(
        id="call_lookup",
        type="function",
        function=ToolCallFunction(name="lookup", arguments=json.dumps({})),
    )

    def first_call(kwargs: MatcherArgs) -> bool:
        return not any(isinstance(msg, ToolMessage) for msg in kwargs["messages"])

    def second_call(kwargs: MatcherArgs) -> bool:
        messages = kwargs["messages"]
        return (
            isinstance(messages[-3], AssistantMessage)
            and isinstance(messages[-2], ToolMessage)
            and isinstance(messages[-1], UserMessage)
            and "[MID-TURN USER UPDATE]" in str(messages[-1].content)
            and "Actually use the newer instruction" in str(messages[-1].content)
        )

    llm_client = RuleBasedMockLLMClient(
        rules=[
            (first_call, LLMOutput(tool_calls=[tool_call])),
            (second_call, LLMOutput(content="updated answer")),
        ]
    )
    service = ProcessingService(
        llm_client=llm_client,
        tools_provider=ToolProvider(),
        service_config=ProcessingServiceConfig(
            prompts={},
            timezone=ZoneInfo("UTC"),
            max_history_messages=5,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.NONE,
            id="mid_turn_test",
        ),
        context_providers=[],
        server_url="http://test",
        app_config=AppConfig(),
    )

    async with get_db_context(engine=db_engine) as db_context:
        generated_messages, _, _ = await service.process_message(
            db_context=db_context,
            messages=[UserMessage(content="start")],
            interface_type="telegram",
            conversation_id="123",
            user_name="TestUser",
            turn_id="turn-1",
            chat_interface=None,
            mid_turn_input_provider=MidTurnProvider([
                MidTurnUserInput(
                    content="Actually use the newer instruction",
                    interface_message_id="102",
                    user_name="TestUser",
                )
            ]),
        )

    assert any(
        isinstance(message, UserMessage)
        and "Actually use the newer instruction" in str(message.content)
        for message in generated_messages
    )
    assert generated_messages[-1] == AssistantMessage(content="updated answer")
