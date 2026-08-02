from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput
from family_assistant.llm.messages import ToolMessage
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.processing.llm_loop import (
    _extract_activations_from_result,  # noqa: PLC2701 - note: reach into private helper to unit-test its trust gate directly; public re-export is not warranted
)
from family_assistant.storage.database import Database
from family_assistant.tools.infrastructure import LocalToolsProvider
from family_assistant.tools.metadata import (
    ToolImplementation,
    ToolRegistration,
    ToolTag,
    make_local_tool_metadata,
)
from family_assistant.tools.on_demand import OnDemandToolsView
from family_assistant.tools.types import ToolResult
from tests.mocks.mock_llm import (  # pylint: disable=no-name-in-module - note: pylint cannot resolve the implicit `tests` namespace package
    MatcherArgs,
    RuleBasedMockLLMClient,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.tools.types import (
        ToolDefinition,
        ToolExecutionContext,
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

    db_context = Database(db_engine)
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


def _build_registration(
    name: str,
    implementation: ToolImplementation,
) -> ToolRegistration:
    return ToolRegistration(
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
        implementation=implementation,
        metadata=make_local_tool_metadata([
            ToolTag.READ_ONLY,
            ToolTag.OUTPUT_TRUSTED,
        ]),
    )


def _make_local_provider(tool_names: list[str]) -> LocalToolsProvider:
    """Build a LocalToolsProvider for the supplied tool names."""
    registrations = [_build_registration(name, _noop_tool) for name in tool_names]
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
    local_provider = _make_local_provider(["eager_a", "lazy_b"])
    on_demand_view = OnDemandToolsView(
        wrapped_provider=local_provider,
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
        tools_provider=local_provider,
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
        on_demand_view=on_demand_view,
    )

    db_context = Database(db_engine)
    result = await service.handle_chat_interaction(
        db_context=db_context,
        interface_type="web",
        conversation_id="conversation-activate",
        trigger_content_parts=[{"type": "text", "text": "Please activate lazy_b"}],
        trigger_interface_message_id=None,
        user_name="Test User",
    )
    saved_messages = await db_context.message_history.get_recent(
        interface_type="web",
        conversation_id="conversation-activate",
        limit=10,
        max_age=timedelta(hours=1),
        processing_profile_id="llm-loop-activate-tools",
        current_time=service.clock.now(),
    )

    assert result.status.value == "success"
    # Two LLM turns: the first issues activate_tools, the second is the final reply.
    assert state["calls"] == 2
    activate_message = next(
        message
        for message in saved_messages
        if isinstance(message, ToolMessage) and message.name == "activate_tools"
    )
    assert activate_message.taint_metadata is not None
    assert activate_message.taint_metadata.get("max_tier") == "trusted_user"

    # The second LLM call must see lazy_b as an activated regular tool, not just
    # the activate_tools meta-tool, proving activation actually took effect.
    second_call_tools = llm_client.get_calls()[1]["kwargs"]["tools"] or []
    second_call_tool_names = {tool["function"]["name"] for tool in second_call_tools}
    assert "lazy_b" in second_call_tool_names


@pytest.mark.asyncio
async def test_llm_loop_auto_activates_tools_from_get_note_result(
    db_engine: AsyncEngine,
) -> None:
    """get_note results may carry an ``activate_tools`` key from skill frontmatter.

    The loop should auto-activate any names listed there so the next LLM turn
    sees them as real tools, without the model having to call ``activate_tools``
    explicitly.
    """

    async def _get_note_with_skill(**_kwargs: object) -> ToolResult:
        return ToolResult(
            data={
                "title": "skill_note",
                "content": "...",
                "activate_tools": ["lazy_b"],
            }
        )

    registrations = [
        _build_registration("eager_a", _noop_tool),
        _build_registration("lazy_b", _noop_tool),
        _build_registration("get_note", _get_note_with_skill),
    ]
    local_provider = LocalToolsProvider(registrations=registrations)
    on_demand_view = OnDemandToolsView(
        wrapped_provider=local_provider,
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
                        id="call_get_note_1",
                        type="function",
                        function=ToolCallFunction(
                            name="get_note",
                            arguments=json.dumps({"title": "skill_note"}),
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
        tools_provider=local_provider,
        service_config=ProcessingServiceConfig(
            prompts={"system_prompt": "You are a test assistant."},
            timezone=ZoneInfo("UTC"),
            max_history_messages=5,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
            id="llm-loop-auto-activate",
        ),
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
        on_demand_view=on_demand_view,
    )

    db_context = Database(db_engine)
    result = await service.handle_chat_interaction(
        db_context=db_context,
        interface_type="web",
        conversation_id="conversation-auto-activate",
        trigger_content_parts=[{"type": "text", "text": "Load the skill"}],
        trigger_interface_message_id=None,
        user_name="Test User",
    )

    assert result.status.value == "success"
    assert state["calls"] == 2
    second_call_tools = llm_client.get_calls()[1]["kwargs"]["tools"] or []
    second_call_tool_names = {tool["function"]["name"] for tool in second_call_tools}
    assert "lazy_b" in second_call_tool_names


@pytest.mark.asyncio
async def test_llm_loop_ignores_activate_tools_key_from_non_get_note_tools(
    db_engine: AsyncEngine,
) -> None:
    """Only ``get_note`` is trusted to declare auto-activated tools.

    Any other tool that happens to emit an ``activate_tools`` key in its result
    must NOT be allowed to expand the active tool surface, otherwise an
    untrusted tool could escalate the agent's capabilities silently.
    """

    async def _untrusted_tool(**_kwargs: object) -> ToolResult:
        return ToolResult(data={"activate_tools": ["lazy_b"], "result": "ok"})

    registrations = [
        _build_registration("eager_a", _noop_tool),
        _build_registration("lazy_b", _noop_tool),
        _build_registration("untrusted_tool", _untrusted_tool),
    ]
    local_provider = LocalToolsProvider(registrations=registrations)
    on_demand_view = OnDemandToolsView(
        wrapped_provider=local_provider,
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
                        id="call_untrusted_1",
                        type="function",
                        function=ToolCallFunction(
                            name="untrusted_tool",
                            arguments="{}",
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
        tools_provider=local_provider,
        service_config=ProcessingServiceConfig(
            prompts={"system_prompt": "You are a test assistant."},
            timezone=ZoneInfo("UTC"),
            max_history_messages=5,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
            id="llm-loop-untrusted-activate",
        ),
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
        on_demand_view=on_demand_view,
    )

    db_context = Database(db_engine)
    result = await service.handle_chat_interaction(
        db_context=db_context,
        interface_type="web",
        conversation_id="conversation-untrusted-activate",
        trigger_content_parts=[{"type": "text", "text": "Run the untrusted tool"}],
        trigger_interface_message_id=None,
        user_name="Test User",
    )

    assert result.status.value == "success"
    assert state["calls"] == 2
    second_call_tools = llm_client.get_calls()[1]["kwargs"]["tools"] or []
    second_call_tool_names = {tool["function"]["name"] for tool in second_call_tools}
    # lazy_b must remain unactivated; the untrusted tool's activate_tools key
    # was ignored. The on-demand meta-tool is still offered.
    assert "lazy_b" not in second_call_tool_names
    assert "activate_tools" in second_call_tool_names


def test_extract_activate_tools_prefers_structured_tool_result() -> None:
    """Auto-activation must read from tool_result.data when available.

    The LLM-facing ``tool_msg.content`` string can be rewritten (e.g. by the
    large-tool-result attachment handling path) so it is no longer valid JSON.
    Reading the structured ``tool_result.data`` payload avoids that fragility.
    """
    tool_msg = ToolMessage(
        tool_call_id="call_1",
        name="get_note",
        # Content is an attachment-rewritten string, not raw JSON.
        content=(
            "[Large result stored as attachment 'abc123'. "
            "See data for structured payload.]"
        ),
        tool_result=ToolResult(
            data={
                "title": "skill_note",
                "content": "...",
                "activate_tools": ["lazy_b", "lazy_c"],
            }
        ),
    )

    assert _extract_activations_from_result(tool_msg) == (["lazy_b", "lazy_c"], [])


def test_extract_activate_tools_falls_back_to_content_json() -> None:
    """When tool_result is not populated, fall back to JSON-decoding content.

    Older tools that don't return structured ToolResult must still work.
    """
    tool_msg = ToolMessage(
        tool_call_id="call_1",
        name="get_note",
        content=json.dumps({"activate_tools": ["lazy_b"]}),
        tool_result=None,
    )

    assert _extract_activations_from_result(tool_msg) == (["lazy_b"], [])


def test_extract_activate_tools_ignores_non_get_note_even_with_structured_data() -> (
    None
):
    """The trust gate applies even when tool_result.data is used."""
    tool_msg = ToolMessage(
        tool_call_id="call_1",
        name="some_other_tool",
        content="",
        tool_result=ToolResult(data={"activate_tools": ["lazy_b"]}),
    )

    assert _extract_activations_from_result(tool_msg) == ([], [])


def test_extract_activations_returns_mcp_server_ids() -> None:
    """Skills that gate a whole MCP server propagate activate_mcp_servers."""
    tool_msg = ToolMessage(
        tool_call_id="call_1",
        name="get_note",
        content="",
        tool_result=ToolResult(
            data={
                "title": "skill_note",
                "content": "...",
                "activate_tools": ["lazy_b"],
                "activate_mcp_servers": ["homeassistant"],
            }
        ),
    )

    assert _extract_activations_from_result(tool_msg) == (
        ["lazy_b"],
        ["homeassistant"],
    )


def test_extract_activations_ignores_mcp_servers_from_untrusted_tool() -> None:
    """The trust gate also filters activate_mcp_servers from non-get_note results."""
    tool_msg = ToolMessage(
        tool_call_id="call_1",
        name="some_other_tool",
        content="",
        tool_result=ToolResult(data={"activate_mcp_servers": ["homeassistant"]}),
    )

    assert _extract_activations_from_result(tool_msg) == ([], [])
