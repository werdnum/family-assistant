"""A provider that reads the web server-side taints the reply it produced."""

from collections.abc import AsyncIterator
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput, LLMStreamEvent, StreamEventMetadata
from family_assistant.llm.messages import LLMMessage
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.database import Database
from family_assistant.storage.message_history import message_history_table
from family_assistant.tools import ToolExecutionContext
from family_assistant.tools.types import ToolDefinition, ToolResult
from tests.mocks.mock_llm import RuleBasedMockLLMClient


class _NoToolsProvider:
    async def get_tool_definitions(self) -> list:
        return []

    async def execute_tool(
        self,
        name: str,
        arguments: dict,
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        raise AssertionError("no tool should be called")

    async def close(self) -> None:
        pass


class _BrowsingAgentClient(RuleBasedMockLLMClient):
    """Streams like a hosted agent that declares what it read server-side."""

    def generate_response_stream(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        inner = super().generate_response_stream(messages, tools, tool_choice)

        async def _stream() -> AsyncIterator[LLMStreamEvent]:
            async for event in inner:
                if event.type != "done":
                    yield event
                    continue
                metadata: StreamEventMetadata = {
                    **(event.metadata or {}),
                    "provider_external_read": {
                        "source_id": "deep_research:inter_1",
                        "reason": "Deep Research read the web.",
                    },
                }
                yield LLMStreamEvent(type="done", metadata=metadata)

        return _stream()


@pytest.mark.asyncio
async def test_provider_external_read_taints_the_reply_not_the_request(
    db_engine: AsyncEngine,
) -> None:
    service = ProcessingService(
        llm_client=_BrowsingAgentClient(
            rules=[], default_response=LLMOutput(content="Findings from the web")
        ),
        tools_provider=_NoToolsProvider(),
        service_config=ProcessingServiceConfig(
            prompts={"system_prompt": "You are a research assistant."},
            timezone=ZoneInfo("UTC"),
            max_history_messages=10,
            history_max_age_hours=24,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
            id="research",
        ),
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
    )

    db = Database(db_engine)
    result = await service.handle_chat_interaction(
        db_context=db,
        interface_type="test",
        conversation_id="research-conv",
        trigger_content_parts=[{"type": "text", "text": "Research this"}],
        trigger_interface_message_id="msg-1",
        user_name="TestUser",
    )
    assert result.text_reply == "Findings from the web"

    rows = await db.fetch_all(
        select(
            message_history_table.c.role, message_history_table.c.taint_metadata_json
        ).where(message_history_table.c.conversation_id == "research-conv")
    )
    tiers = {row["role"]: row["taint_metadata_json"]["max_tier"] for row in rows}
    assert tiers["assistant"] == "unknown_external"
    assert tiers["user"] != "unknown_external"
    assistant_sources = next(
        row["taint_metadata_json"]["sources"]
        for row in rows
        if row["role"] == "assistant"
    )
    assert ("tool_output", "deep_research:inter_1") in {
        (source["source_type"], source["source_id"]) for source in assistant_sources
    }
