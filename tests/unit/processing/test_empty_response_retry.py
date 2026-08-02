"""
Tests that empty LLM responses (no content, no tool calls) trigger a retry.
"""

import logging
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.database import Database
from family_assistant.tools import ToolExecutionContext
from family_assistant.tools.types import ToolResult
from tests.mocks.mock_llm import MatcherArgs, RuleBasedMockLLMClient


class SimpleToolsProvider:
    async def get_tool_definitions(self) -> list:
        return []

    async def execute_tool(
        self,
        name: str,
        arguments: dict,
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        return ""

    async def close(self) -> None:
        pass


def _make_service(llm_client: RuleBasedMockLLMClient) -> ProcessingService:
    config = ProcessingServiceConfig(
        prompts={"system_prompt": "You are a helpful assistant."},
        timezone=ZoneInfo("UTC"),
        max_history_messages=10,
        history_max_age_hours=24,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.CONFIRM,
        id="test_empty_response",
    )
    return ProcessingService(
        llm_client=llm_client,
        tools_provider=SimpleToolsProvider(),
        service_config=config,
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
    )


@pytest.mark.asyncio
async def test_empty_response_retries_and_succeeds(
    db_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """When the LLM returns an empty response first, then a real response on retry,
    the final result should contain the real response content."""
    call_count = 0

    def response_generator(kwargs: MatcherArgs) -> LLMOutput:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return LLMOutput(content=None, tool_calls=None)
        return LLMOutput(content="Here is my response")

    llm_client = RuleBasedMockLLMClient(rules=[(lambda _: True, response_generator)])
    service = _make_service(llm_client)

    with caplog.at_level(logging.WARNING, logger="family_assistant.processing"):
        db_context = Database(db_engine)
        result = await service.handle_chat_interaction(
            db_context=db_context,
            interface_type="test",
            conversation_id="test-conv-1",
            trigger_content_parts=[{"type": "text", "text": "Hello"}],
            trigger_interface_message_id="msg-1",
            user_name="TestUser",
        )

    assert result.text_reply == "Here is my response"
    assert call_count == 2
    assert any(
        "LLM returned empty response" in record.message
        and "Re-prompting" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_empty_response_retry_exhausted_still_returns(
    db_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """When the LLM returns empty responses on both attempts, we still proceed
    (the response will be empty but no crash)."""
    llm_client = RuleBasedMockLLMClient(
        rules=[],
        default_response=LLMOutput(content=None, tool_calls=None),
    )
    service = _make_service(llm_client)

    with caplog.at_level(logging.WARNING, logger="family_assistant.processing"):
        db_context = Database(db_engine)
        result = await service.handle_chat_interaction(
            db_context=db_context,
            interface_type="test",
            conversation_id="test-conv-2",
            trigger_content_parts=[{"type": "text", "text": "Hello"}],
            trigger_interface_message_id="msg-2",
            user_name="TestUser",
        )

    # Both attempts were empty; response should be None/empty
    assert not result.text_reply
    warning_messages = [
        r.message for r in caplog.records if "LLM returned empty response" in r.message
    ]
    assert len(warning_messages) == 2
    assert any("Re-prompting" in m for m in warning_messages)
    assert any("Proceeding with empty response" in m for m in warning_messages)
