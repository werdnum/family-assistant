from __future__ import annotations

# pylint: disable=no-name-in-module
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.tools import MCPToolsProvider, ToolDescriptor
from family_assistant.web.routers.chat_api import chat_api_router
from tests.mocks.mock_llm import RuleBasedMockLLMClient

if TYPE_CHECKING:
    from family_assistant.tools.types import (
        MCPServerConfig,
        ToolDefinition,
        ToolExecutionContext,
        ToolResult,
    )


class EmptyDescriptorWrappingProvider:
    def __init__(self, wrapped_provider: MCPToolsProvider) -> None:
        self.wrapped_provider = wrapped_provider

    async def get_tool_definitions(self) -> list[ToolDefinition]:
        return [
            cast(
                "ToolDefinition",
                {
                    "type": "function",
                    "function": {
                        "name": "local_tool",
                        "description": "A local tool",
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
            )
        ]

    async def get_tool_descriptors(self) -> list[ToolDescriptor]:
        return []

    async def get_tool_descriptor(self, name: str) -> ToolDescriptor | None:
        return None

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, object],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        return "ok"

    async def close(self) -> None:
        await self.wrapped_provider.close()


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_profiles_api_does_not_fallback_to_all_mcp_servers_when_none_visible() -> (
    None
):
    mcp_provider = MCPToolsProvider(
        mcp_server_configs={
            "time": cast(
                "MCPServerConfig",
                {
                    "command": "echo",
                    "args": [],
                },
            ),
            "brave": cast(
                "MCPServerConfig",
                {
                    "command": "echo",
                    "args": [],
                },
            ),
        },
        initialization_timeout_seconds=1,
    )
    tools_provider = EmptyDescriptorWrappingProvider(mcp_provider)
    processing_service = ProcessingService(
        llm_client=RuleBasedMockLLMClient(
            rules=[],
            default_response=LLMOutput(content="ok", tool_calls=None),
        ),
        tools_provider=tools_provider,
        service_config=ProcessingServiceConfig(
            prompts={"system_prompt": "You are a test assistant."},
            timezone=ZoneInfo("UTC"),
            max_history_messages=5,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
            id="profile-no-mcp",
            description="Profile without visible MCP tools",
        ),
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
    )

    app = FastAPI()
    app.include_router(chat_api_router, prefix="/api")
    app.state.processing_service = processing_service
    app.state.processing_services = {"profile-no-mcp": processing_service}
    app.state.config = AppConfig(default_service_profile_id="profile-no-mcp")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/v1/profiles")

    assert response.status_code == 200
    body = response.json()
    assert body["default_profile_id"] == "profile-no-mcp"
    assert body["profiles"][0]["enabled_mcp_servers"] == []
