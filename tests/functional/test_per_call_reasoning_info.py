"""Each LLM call's cost and timing is saved with the message it produced.

A turn is not one call: a tool loop makes one per iteration. The row for each
assistant message must therefore carry that call's own numbers, or a
tool-heavy turn reports the last call's tokens as though they were the whole
turn's.
"""

from collections.abc import Sequence
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput, ToolCallFunction, ToolCallItem
from family_assistant.llm.content_parts import text_content
from family_assistant.llm.messages import LLMMessage
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.database import Database
from family_assistant.tools import LocalToolsProvider
from family_assistant.tools.types import ToolDefinition, ToolResult
from tests.mocks.mock_llm import (  # pylint: disable=no-name-in-module
    RuleBasedMockLLMClient,
)

_ECHO_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": "echo",
        "description": "Echo a value back.",
        "parameters": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
    },
}


class _ScriptedLLMClient(RuleBasedMockLLMClient):
    """Returns a queued output per call, so each call is distinguishable."""

    def __init__(self, outputs: Sequence[LLMOutput]) -> None:
        super().__init__(rules=[], default_response=LLMOutput(content="unused"))
        self._outputs = list(outputs)

    async def generate_response(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        del messages, tools, tool_choice
        return self._outputs.pop(0) if self._outputs else LLMOutput(content="done")


async def _echo(value: str, **_kwargs: Any) -> ToolResult:  # noqa: ANN401
    return ToolResult(text=value)


def _make_service(client: RuleBasedMockLLMClient) -> ProcessingService:
    return ProcessingService(
        llm_client=client,
        tools_provider=LocalToolsProvider(
            definitions=[_ECHO_TOOL], implementations={"echo": _echo}
        ),
        service_config=ProcessingServiceConfig(
            prompts={"system_prompt": "You are a test assistant."},
            timezone=ZoneInfo("UTC"),
            max_history_messages=5,
            history_max_age_hours=24,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.BLOCKED,
            id="test_profile",
        ),
        context_providers=[],
        server_url="http://localhost:8000",
        app_config=AppConfig(),
        credential_resolvers=None,
        api_backend=None,
    )


@pytest.mark.asyncio
async def test_each_assistant_message_keeps_its_own_calls_usage(
    db_engine: AsyncEngine,
) -> None:
    """A tool loop's first call must not be recorded as costing nothing."""
    client = _ScriptedLLMClient([
        LLMOutput(
            tool_calls=[
                ToolCallItem(
                    id="call_1",
                    type="function",
                    function=ToolCallFunction(
                        name="echo", arguments='{"value": "pong"}'
                    ),
                )
            ],
            reasoning_info={"prompt_tokens": 100, "completion_tokens": 10},
        ),
        LLMOutput(
            content="pong",
            reasoning_info={"prompt_tokens": 200, "completion_tokens": 20},
        ),
    ])
    service = _make_service(client)
    db_context = Database(engine=db_engine)

    await service.handle_chat_interaction(
        db_context=db_context,
        interface_type="test",
        conversation_id="per-call-usage",
        trigger_content_parts=[text_content("ping")],
        trigger_interface_message_id="msg-1",
        user_name="TestUser",
    )

    rows = await db_context.message_history.get_recent_with_metadata(
        interface_type="test", conversation_id="per-call-usage"
    )
    assistant_usage = [
        row["reasoning_info"] for row in rows if row["role"] == "assistant"
    ]

    assert len(assistant_usage) == 2, "one assistant row per LLM call"
    assert all(usage is not None for usage in assistant_usage)
    # Each row carries its own call, not the turn's last one twice over.
    prompt_tokens = sorted(
        tokens
        for usage in assistant_usage
        if usage is not None and (tokens := usage.get("prompt_tokens")) is not None
    )
    assert prompt_tokens == [100, 200]
