import json
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import (
    AssistantMessage,
    LLMMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.processing.types import MidTurnUserInput
from family_assistant.security.taint import (
    SinkClass,
    SourceTrustTier,
    TurnTaintState,
)
from family_assistant.services.tool_call_review import (
    ToolCallReviewConstraints,
    ToolCallReviewInput,
    ToolCallReviewVerdict,
    assemble_tool_call_review_messages,
    compute_trusted_destination_echo,
)
from family_assistant.storage.database import Database
from family_assistant.tools.metadata import ToolDescriptor
from family_assistant.tools.types import ToolDefinition, ToolExecutionContext
from tests.mocks.mock_llm import (  # pylint: disable=no-name-in-module
    LLMOutput,
    MatcherArgs,
    RuleBasedMockLLMClient,
)


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
    destination = "https://example.test/tomorrow"
    steer_text = f"Actually use the newer instruction and send it to {destination}"
    messages_after_steer: list[LLMMessage] = []
    tool_call = ToolCallItem(
        id="call_lookup",
        type="function",
        function=ToolCallFunction(name="lookup", arguments=json.dumps({})),
    )

    def first_call(kwargs: MatcherArgs) -> bool:
        return not any(isinstance(msg, ToolMessage) for msg in kwargs["messages"])

    def second_call(kwargs: MatcherArgs) -> bool:
        messages = kwargs["messages"]
        messages_after_steer[:] = messages
        return (
            isinstance(messages[-3], AssistantMessage)
            and isinstance(messages[-2], ToolMessage)
            and isinstance(messages[-1], UserMessage)
            and "[MID-TURN USER UPDATE]" in str(messages[-1].content)
            and steer_text in str(messages[-1].content)
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

    db_context = Database(engine=db_engine)
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
                content=steer_text,
                interface_message_id="102",
                user_name="TestUser",
            )
        ]),
    )

    assert any(
        isinstance(message, UserMessage) and steer_text in str(message.content)
        for message in generated_messages
    )
    persisted_steer = next(
        message
        for message in generated_messages
        if isinstance(message, UserMessage) and message.content == steer_text
    )
    assert (
        TurnTaintState.from_metadata(persisted_steer.taint_metadata).max_tier
        is SourceTrustTier.TRUSTED_USER
    )

    model_steer = messages_after_steer[-1]
    assert isinstance(model_steer, UserMessage)
    assert (
        TurnTaintState.from_metadata(model_steer.taint_metadata).max_tier
        is SourceTrustTier.TRUSTED_USER
    )
    destination_echo = compute_trusted_destination_echo(
        destination,
        messages_after_steer,
    )
    assert destination_echo is not None
    assert destination_echo.matched is True

    review_messages = assemble_tool_call_review_messages(
        ToolCallReviewInput(
            messages=messages_after_steer,
            descriptor=ToolDescriptor(
                name="send_test",
                definition={
                    "type": "function",
                    "function": {
                        "name": "send_test",
                        "description": "Send test data",
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
                tags=frozenset(),
                origin="local",
            ),
            arguments={"destination": destination},
            sink_class=SinkClass.ARBITRARY_EXTERNAL_MESSAGE,
            taint_state=TurnTaintState.empty(),
            policy_contexts=(),
        ),
        ToolCallReviewConstraints(
            fallback_verdict=ToolCallReviewVerdict.CONFIRM,
        ),
    )
    review_prompt = str(review_messages[-1].content)
    assert steer_text in review_prompt
    assert '<trusted_conversation index="' in review_prompt
    assert isinstance(generated_messages[-1], AssistantMessage)
    assert generated_messages[-1].content == "updated answer"
    assert generated_messages[-1].taint_metadata is not None
    assert generated_messages[-1].taint_metadata.get("max_tier") == "trusted_internal"
