from __future__ import annotations

# pylint: disable=no-name-in-module
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.context import get_db_context
from tests.mocks.mock_llm import RuleBasedMockLLMClient

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
