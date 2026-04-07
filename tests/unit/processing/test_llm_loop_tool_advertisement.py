from __future__ import annotations

# pylint: disable=no-name-in-module
import json
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.context import get_db_context
from family_assistant.tools.infrastructure import LocalToolsProvider
from family_assistant.tools.metadata import (
    ToolRegistration,
    ToolTag,
    make_local_tool_metadata,
)
from family_assistant.tools.on_demand import OnDemandAwareToolsProvider
from tests.mocks.mock_llm import MatcherArgs, RuleBasedMockLLMClient

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.tools.types import (
        ToolDefinition,
        ToolExecutionContext,
        ToolResult,
    )


class ConfirmationAwareStubToolsProvider:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    async def get_tool_definitions(
        self,
        *,
        can_confirm: bool = True,
    ) -> list[ToolDefinition]:
        self.calls.append(can_confirm)
        tools: list[ToolDefinition] = [
            cast(
                "ToolDefinition",
                {
                    "type": "function",
                    "function": {
                        "name": "safe_tool",
                        "description": "Always allowed",
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
            )
        ]
        if can_confirm:
            tools.append(
                cast(
                    "ToolDefinition",
                    {
                        "type": "function",
                        "function": {
                            "name": "confirm_tool",
                            "description": "Requires confirmation",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    },
                )
            )
        return tools

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, object],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        return f"Executed {name}"

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("has_confirmation_callback", "expected_tools"),
    [
        (False, ["safe_tool"]),
        (True, ["confirm_tool", "safe_tool"]),
    ],
)
async def test_llm_loop_requests_confirmation_aware_tool_advertisement(
    db_engine: AsyncEngine,
    has_confirmation_callback: bool,
    expected_tools: list[str],
) -> None:
    tools_provider = ConfirmationAwareStubToolsProvider()
    llm_client = RuleBasedMockLLMClient(
        rules=[],
        default_response=LLMOutput(content="ok", tool_calls=None),
    )
    service = ProcessingService(
        llm_client=llm_client,
        tools_provider=tools_provider,
        service_config=ProcessingServiceConfig(
            prompts={"system_prompt": "You are a test assistant."},
            timezone=ZoneInfo("UTC"),
            max_history_messages=5,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
            id="llm-loop-tools",
        ),
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
    )

    callback_fn: object | None = None
    if has_confirmation_callback:

        def _callback(**_: object) -> bool:
            return True

        callback_fn = _callback

    async with get_db_context(db_engine) as db_context:
        result = await service.handle_chat_interaction(
            db_context=db_context,
            interface_type="web",
            conversation_id="conversation-1",
            trigger_content_parts=[{"type": "text", "text": "Hello"}],
            trigger_interface_message_id=None,
            user_name="Test User",
            request_confirmation_callback=cast("Any", callback_fn),
        )

    assert result.status.value == "success"
    assert tools_provider.calls == [has_confirmation_callback]
    assert len(llm_client.get_calls()) == 1
    tool_names = sorted(
        tool["function"]["name"]
        for tool in llm_client.get_calls()[0]["kwargs"]["tools"] or []
    )
    assert tool_names == expected_tools


async def _noop_tool(**_kwargs: object) -> str:
    return "ok"


def _make_local_provider(tool_names: list[str]) -> LocalToolsProvider:
    """Build a LocalToolsProvider for the supplied tool names."""
    registrations = [
        ToolRegistration(
            definition=cast(
                "ToolDefinition",
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": f"Description of {name}.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
            ),
            implementation=_noop_tool,
            metadata=make_local_tool_metadata([
                ToolTag.READ_ONLY,
                ToolTag.OUTPUT_TRUSTED,
            ]),
        )
        for name in tool_names
    ]
    return LocalToolsProvider(registrations=registrations)


@pytest.mark.asyncio
async def test_llm_loop_executes_activate_tools_call_end_to_end(
    db_engine: AsyncEngine,
) -> None:
    """The loop must dispatch activate_tools when the LLM returns it as a tool call.

    Regression test: previously the loop read
    ``tool_call.function.parsed_arguments``, which does not exist on
    ``ToolCallFunction``. The first activate_tools call would raise
    ``AttributeError`` and crash the turn. The loop now parses
    ``function.arguments`` (which may be a JSON string from the model) before
    reading ``tool_names``/``search``.
    """
    on_demand_provider = OnDemandAwareToolsProvider(
        wrapped_provider=_make_local_provider(["eager_a", "lazy_b"]),
        on_demand_tool_names={"lazy_b"},
    )

    state = {"calls": 0}

    def _matcher(_args: MatcherArgs) -> bool:
        return True

    def _response(_args: MatcherArgs) -> LLMOutput:
        state["calls"] += 1
        if state["calls"] == 1:
            return LLMOutput(
                content=None,
                tool_calls=[
                    ToolCallItem(
                        id="call_activate_1",
                        type="function",
                        function=ToolCallFunction(
                            name="activate_tools",
                            arguments=json.dumps({"tool_names": ["lazy_b"]}),
                        ),
                    )
                ],
            )
        return LLMOutput(content="all done", tool_calls=None)

    llm_client = RuleBasedMockLLMClient(
        rules=[(_matcher, _response)],
        default_response=LLMOutput(content="fallback", tool_calls=None),
    )
    service = ProcessingService(
        llm_client=llm_client,
        tools_provider=on_demand_provider,
        service_config=ProcessingServiceConfig(
            prompts={"system_prompt": "You are a test assistant."},
            timezone=ZoneInfo("UTC"),
            max_history_messages=5,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
            id="llm-loop-activate-tools",
        ),
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
    )

    async with get_db_context(db_engine) as db_context:
        result = await service.handle_chat_interaction(
            db_context=db_context,
            interface_type="web",
            conversation_id="conversation-activate",
            trigger_content_parts=[{"type": "text", "text": "Please activate lazy_b"}],
            trigger_interface_message_id=None,
            user_name="Test User",
        )

    assert result.status.value == "success"
    # Two LLM turns: the first issues activate_tools, the second is the final reply.
    assert state["calls"] == 2

    # The second LLM call must see lazy_b as an activated regular tool, not just
    # the activate_tools meta-tool, proving activation actually took effect.
    second_call_tools = llm_client.get_calls()[1]["kwargs"]["tools"] or []
    second_call_tool_names = {tool["function"]["name"] for tool in second_call_tools}
    assert "lazy_b" in second_call_tool_names
