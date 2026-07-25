"""The tool-call loop must not rewrite the system prompt between iterations.

The system prompt renders at the front of the provider prompt prefix, so any
per-iteration edit invalidates the prompt cache for the whole turn -- prompt,
tools, and every accumulated tool result get re-read at full price on each tool
call. These tests pin the prompt as byte-stable across iterations.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput
from family_assistant.llm.base import ContextLengthError
from family_assistant.llm.messages import SystemMessage, UserMessage
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.context import get_db_context
from family_assistant.tools.types import ToolAttachment, ToolResult
from tests.mocks.mock_llm import (  # pylint: disable=no-name-in-module
    RuleBasedMockLLMClient,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.llm.messages import LLMMessage
    from family_assistant.tools.types import ToolDefinition, ToolExecutionContext


class _EchoToolsProvider:
    async def get_tool_definitions(
        self, *, can_confirm: bool = True
    ) -> list[ToolDefinition]:
        return [
            cast(
                "ToolDefinition",
                {
                    "type": "function",
                    "function": {
                        "name": "echo_tool",
                        "description": "Echoes back.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
            )
        ]

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - tool args are dynamic
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        return "echoed"

    async def close(self) -> None:
        pass


class _SnapshottingMockLLMClient(RuleBasedMockLLMClient):
    """Records the exact prompt seen on each call.

    The loop mutates its message list in place, so the shared recorder in
    RuleBasedMockLLMClient would hand every call the same final list. These
    snapshots are taken per call instead.
    """

    def __init__(self, tool_call_rounds: int) -> None:
        super().__init__(rules=[], default_response=LLMOutput(content="done"))
        self._tool_call_rounds = tool_call_rounds
        self._round = 0
        self.system_prompts: list[str] = []
        self.trailing_messages: list[LLMMessage] = []
        self.retry_messages: list[LLMMessage] = []

    async def generate_response(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        first = messages[0]
        self.system_prompts.append(
            first.content if isinstance(first, SystemMessage) else ""
        )
        self.trailing_messages.append(messages[-1])
        self.retry_messages = list(messages)

        self._round += 1
        if self._round <= self._tool_call_rounds:
            return LLMOutput(
                content=None,
                tool_calls=[
                    ToolCallItem(
                        id=f"call_{self._round}",
                        type="function",
                        function=ToolCallFunction(
                            name="echo_tool", arguments=json.dumps({})
                        ),
                    )
                ],
            )
        return LLMOutput(content="done")


def _make_service(
    llm_client: RuleBasedMockLLMClient,
    max_iterations: int,
) -> ProcessingService:
    return ProcessingService(
        llm_client=llm_client,
        tools_provider=_EchoToolsProvider(),
        service_config=ProcessingServiceConfig(
            prompts={"system_prompt": "You are a test assistant. {current_time}"},
            timezone=ZoneInfo("UTC"),
            max_history_messages=5,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
            id="llm-loop-cache",
            max_iterations=max_iterations,
        ),
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
    )


@pytest.mark.asyncio
async def test_system_prompt_is_byte_stable_across_tool_iterations(
    db_engine: AsyncEngine,
) -> None:
    llm_client = _SnapshottingMockLLMClient(tool_call_rounds=2)
    service = _make_service(llm_client, max_iterations=5)

    async with get_db_context(db_engine) as db_context:
        result = await service.handle_chat_interaction(
            db_context=db_context,
            interface_type="web",
            conversation_id="cache-stability",
            trigger_content_parts=[{"type": "text", "text": "Hello"}],
            trigger_interface_message_id=None,
            user_name="Test User",
        )

    assert result.status.value == "success"
    assert len(llm_client.system_prompts) == 3
    assert len(set(llm_client.system_prompts)) == 1


@pytest.mark.asyncio
async def test_system_prompt_carries_no_iteration_counter(
    db_engine: AsyncEngine,
) -> None:
    llm_client = _SnapshottingMockLLMClient(tool_call_rounds=1)
    service = _make_service(llm_client, max_iterations=5)

    async with get_db_context(db_engine) as db_context:
        await service.handle_chat_interaction(
            db_context=db_context,
            interface_type="web",
            conversation_id="cache-no-counter",
            trigger_content_parts=[{"type": "text", "text": "Hello"}],
            trigger_interface_message_id=None,
            user_name="Test User",
        )

    assert llm_client.system_prompts
    for prompt in llm_client.system_prompts:
        assert "Processing iteration" not in prompt


@pytest.mark.asyncio
async def test_final_iteration_instruction_is_appended_not_prepended(
    db_engine: AsyncEngine,
) -> None:
    """Delivering it as a trailing user message keeps the cached prefix intact."""
    llm_client = _SnapshottingMockLLMClient(tool_call_rounds=0)
    service = _make_service(llm_client, max_iterations=1)

    async with get_db_context(db_engine) as db_context:
        await service.handle_chat_interaction(
            db_context=db_context,
            interface_type="web",
            conversation_id="cache-final-iteration",
            trigger_content_parts=[{"type": "text", "text": "Hello"}],
            trigger_interface_message_id=None,
            user_name="Test User",
        )

    assert llm_client.system_prompts
    assert "final processing iteration" not in llm_client.system_prompts[0].lower()

    trailing = llm_client.trailing_messages[0]
    assert isinstance(trailing, UserMessage)
    assert "final processing iteration" in str(trailing.content).lower()


@pytest.mark.asyncio
async def test_system_prompt_keeps_its_cache_breakpoint_through_the_loop(
    db_engine: AsyncEngine,
) -> None:
    """A rebuilt system message must not silently drop the breakpoint offset."""
    llm_client = _SnapshottingMockLLMClient(tool_call_rounds=1)
    service = _make_service(llm_client, max_iterations=5)

    captured: list[SystemMessage] = []
    original = llm_client.generate_response

    async def _capture(
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        if isinstance(messages[0], SystemMessage):
            captured.append(messages[0])
        return await original(messages, tools, tool_choice)

    llm_client.generate_response = _capture  # type: ignore[method-assign]

    async with get_db_context(db_engine) as db_context:
        await service.handle_chat_interaction(
            db_context=db_context,
            interface_type="web",
            conversation_id="cache-breakpoint-survives",
            trigger_content_parts=[{"type": "text", "text": "Hello"}],
            trigger_interface_message_id=None,
            user_name="Test User",
        )

    assert captured
    for system_message in captured:
        assert system_message.stable_prefix_len is not None
        assert 0 < system_message.stable_prefix_len <= len(system_message.content)


class _AttachingToolsProvider(_EchoToolsProvider):
    """Returns a tool result carrying more attachments than the selection threshold."""

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - tool args are dynamic
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        return ToolResult(
            text="attached",
            attachments=[
                ToolAttachment(
                    mime_type="text/plain",
                    description=f"file {index}",
                    attachment_id=f"att{index}",
                )
                for index in range(4)
            ],
        )


@pytest.mark.asyncio
async def test_attachment_selection_uses_the_users_request_not_the_scaffolding(
    db_engine: AsyncEngine,
) -> None:
    """The synthetic final-iteration instruction is the newest user message on the
    last iteration. Selecting attachments against it would match boilerplate
    instead of what the user actually asked for."""
    llm_client = _SnapshottingMockLLMClient(tool_call_rounds=1)
    service = _make_service(llm_client, max_iterations=2)
    service.tool_executor.tools_provider = _AttachingToolsProvider()
    service.llm_loop.tool_executor.tools_provider = _AttachingToolsProvider()
    service.app_config.attachment_selection_threshold = 1

    queries: list[str] = []

    async def _capture_query(
        pending_attachment_ids: list[str],
        original_query: str,
        *,
        acting_user_id: str | None,
    ) -> list[str]:
        queries.append(original_query)
        return pending_attachment_ids

    service.llm_loop.attachment_processor.select_for_response = _capture_query  # type: ignore[method-assign]

    async with get_db_context(db_engine) as db_context:
        await service.handle_chat_interaction(
            db_context=db_context,
            interface_type="web",
            conversation_id="cache-attachment-query",
            trigger_content_parts=[
                {"type": "text", "text": "Show me the pictures of the cat"}
            ],
            trigger_interface_message_id=None,
            user_name="Test User",
        )

    assert queries, "attachment selection never ran"
    for query in queries:
        assert "final processing iteration" not in query.lower()
    assert queries[0] == "Show me the pictures of the cat"


class _ContextLimitOnceMockLLMClient(_SnapshottingMockLLMClient):
    """Raises ContextLengthError once, then succeeds, to drive the pruning retry."""

    def __init__(self) -> None:
        super().__init__(tool_call_rounds=0)
        self._raised = False

    async def generate_response(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        if not self._raised:
            self._raised = True
            raise ContextLengthError(
                "context length exceeded", provider="mock", model="mock"
            )
        return await super().generate_response(messages, tools, tool_choice)


@pytest.mark.asyncio
async def test_context_pruning_keeps_the_user_turn_not_the_scaffolding(
    db_engine: AsyncEngine,
) -> None:
    """The turn splitter starts a turn at every UserMessage, so the synthetic
    final-iteration instruction must be excluded from pruning. At min_turns=1 it
    would otherwise be the only turn kept, dropping the user's actual request."""
    llm_client = _ContextLimitOnceMockLLMClient()
    service = _make_service(llm_client, max_iterations=1)
    service.llm_loop.config.context_pruning_min_turns = 1

    async with get_db_context(db_engine) as db_context:
        await service.handle_chat_interaction(
            db_context=db_context,
            interface_type="web",
            conversation_id="cache-pruning",
            trigger_content_parts=[{"type": "text", "text": "Remember the milk"}],
            trigger_interface_message_id=None,
            user_name="Test User",
        )

    # The retry is the only recorded call: the first attempt raised.
    assert llm_client.retry_messages, "pruning retry never happened"
    texts = [str(msg.content) for msg in llm_client.retry_messages]
    assert any("Remember the milk" in text for text in texts), (
        f"user request was pruned away, leaving only: {texts}"
    )
    assert "final processing iteration" in texts[-1].lower()
