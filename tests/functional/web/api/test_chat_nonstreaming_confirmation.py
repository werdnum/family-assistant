"""Non-streaming chat API: a confirm-policy tool records a durable confirmation.

The non-streaming ``/api/v1/chat/send_message`` endpoint cannot wait on a live
confirmation channel (it is used by headless callers such as iOS App Intents /
Siri). Instead, a tool that needs approval must leave a durable pending
confirmation that the user can approve later from another client. This verifies
that wiring end-to-end through the real ProcessingService and tool policy.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.context_providers import NotesContextProvider
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import (
    LLMInterface,
    LLMOutput,
    ToolCallFunction,
    ToolCallItem,
)
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage import init_db
from family_assistant.storage.confirmation_requests import confirmation_requests_table
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.tools import (
    LOCAL_TOOL_REGISTRATIONS as local_tool_registrations,
)
from family_assistant.tools import (
    CompositeToolsProvider,
    LocalToolsProvider,
    MCPToolsProvider,
    PolicyEnforcingToolsProvider,
    PolicyEngine,
    PolicyRule,
    ToolMatcher,
    ToolPolicyConfig,
    ToolPolicyDecision,
    ToolsProvider,
)
from family_assistant.web.app_creator import app as actual_app
from family_assistant.web.web_chat_interface import WebChatInterface
from tests.mocks.mock_llm import MatcherArgs, RuleBasedMockLLMClient

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.tools.types import CalendarConfig


@pytest.fixture
def mock_llm_client() -> RuleBasedMockLLMClient:
    return RuleBasedMockLLMClient(rules=[])


@pytest.fixture
def processing_service_config() -> ProcessingServiceConfig:
    return ProcessingServiceConfig(
        prompts={"system_prompt": "Test assistant. {current_time} {server_url}"},
        timezone=ZoneInfo("UTC"),
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.CONFIRM,
        id="chat_api_test_profile",
    )


@pytest_asyncio.fixture
async def confirm_policy_tools_provider() -> ToolsProvider:
    """Tools provider that requires confirmation for ``add_or_update_note``."""
    local_provider = LocalToolsProvider(
        registrations=local_tool_registrations,
        embedding_generator=None,
        calendar_config=cast("CalendarConfig", {}),
    )
    mock_mcp_provider = AsyncMock(spec=MCPToolsProvider)
    mock_mcp_provider.get_tool_definitions.return_value = []
    composite = CompositeToolsProvider(providers=[local_provider, mock_mcp_provider])
    await composite.get_tool_definitions()

    policy_provider = PolicyEnforcingToolsProvider(
        wrapped_provider=composite,
        policy_engine=PolicyEngine.from_policy_config(
            ToolPolicyConfig(
                default_decision=ToolPolicyDecision.ALLOW,
                rules=[
                    PolicyRule(
                        match=ToolMatcher(names=["add_or_update_note"]),
                        decision=ToolPolicyDecision.CONFIRM,
                        priority=10,
                        description="require confirmation for note writes",
                    )
                ],
            )
        ),
    )
    await policy_provider.get_tool_definitions()
    return policy_provider


@pytest.fixture
def processing_service(
    mock_llm_client: RuleBasedMockLLMClient,
    confirm_policy_tools_provider: ToolsProvider,
    processing_service_config: ProcessingServiceConfig,
    db_engine: AsyncEngine,
) -> ProcessingService:
    async def get_db() -> DatabaseContext:
        async with get_db_context(engine=db_engine) as ctx:
            return ctx

    notes_provider = NotesContextProvider(
        get_db_context_func=get_db,
        prompts=processing_service_config.prompts,
    )
    return ProcessingService(
        llm_client=mock_llm_client,
        tools_provider=confirm_policy_tools_provider,
        service_config=processing_service_config,
        context_providers=[notes_provider],
        server_url="http://testserver",
        app_config=AppConfig(),
        google_credentials=None,
        google_api_backend=None,
    )


@pytest_asyncio.fixture
async def app_fixture(
    db_engine: AsyncEngine,
    processing_service: ProcessingService,
    confirm_policy_tools_provider: ToolsProvider,
    mock_llm_client: LLMInterface,
) -> FastAPI:
    app = FastAPI(middleware=actual_app.user_middleware)
    app.include_router(actual_app.router)
    app.state.processing_service = processing_service
    app.state.tools_provider = confirm_policy_tools_provider
    app.state.database_engine = db_engine
    app.state.config = AppConfig(database_url=str(db_engine.url))
    app.state.llm_client = mock_llm_client
    app.state.debug_mode = False
    app.state.web_chat_interface = WebChatInterface(db_engine)
    async with get_db_context(engine=db_engine) as temp:
        await init_db(db_engine)
        await temp.init_vector_db()
    return app


@pytest_asyncio.fixture
async def test_client(app_fixture: FastAPI) -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=app_fixture)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.mark.asyncio
async def test_confirm_policy_tool_records_durable_confirmation(
    test_client: AsyncClient,
    mock_llm_client: RuleBasedMockLLMClient,
    db_engine: AsyncEngine,
) -> None:
    # First LLM turn calls the confirm-gated tool; the second turn (after the
    # deferred tool result is fed back) produces the final reply.
    def calls_tool(args: MatcherArgs) -> bool:
        messages = args.get("messages", [])
        return not any(msg.role == "tool" for msg in messages)

    def after_tool(args: MatcherArgs) -> bool:
        messages = args.get("messages", [])
        return any(msg.role == "tool" for msg in messages)

    mock_llm_client.rules.append((
        calls_tool,
        LLMOutput(
            content=None,
            tool_calls=[
                ToolCallItem(
                    id="note_call_1",
                    type="function",
                    function=ToolCallFunction(
                        name="add_or_update_note",
                        arguments=json.dumps({"title": "Trip", "content": "Lands 6pm"}),
                    ),
                )
            ],
        ),
    ))
    mock_llm_client.rules.append((
        after_tool,
        LLMOutput(content="I've asked for your approval to save that note."),
    ))

    response = await test_client.post(
        "/api/v1/chat/send_message",
        json={"prompt": "Save a note about my trip", "interface_type": "ios"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["reply"] == "I've asked for your approval to save that note."

    # A durable pending confirmation was recorded for later approval.
    async with get_db_context(engine=db_engine) as db:
        rows = await db.fetch_all(
            select(
                confirmation_requests_table.c.tool_name,
                confirmation_requests_table.c.status,
            )
        )
    assert [(row["tool_name"], row["status"]) for row in rows] == [
        ("add_or_update_note", "pending")
    ]
