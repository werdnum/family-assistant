"""The MCP adapter: ``ask_family_assistant`` over Streamable HTTP at ``/api/mcp``.

Drives the mounted endpoint with the official MCP Python client against the
mock LLM, so what is verified is what claude.ai or Claude Code would see.
"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import TextContent
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, MCPAdapterConfig
from family_assistant.llm import LLMOutput
from family_assistant.llm.messages import UserMessage
from family_assistant.processing import ProcessingService
from family_assistant.storage.database import Database
from family_assistant.web.mcp_adapter import MCPAdapter, install_mcp_adapter
from family_assistant.web.web_chat_interface import WebChatInterface
from tests.mocks.mock_llm import MatcherArgs, RuleBasedMockLLMClient

MCP_URL = "http://testserver/api/mcp"


def _configure_adapter(
    app_fixture: FastAPI, db_engine: AsyncEngine, adapter: MCPAdapterConfig
) -> None:
    app_fixture.state.config = AppConfig(
        database_url=str(db_engine.url),
        server_url="http://localhost:8000",
        mcp_adapter=adapter,
    )


@pytest.fixture
def mcp_app(
    app_fixture: FastAPI,
    api_test_processing_service: ProcessingService,
    db_engine: AsyncEngine,
) -> FastAPI:
    """The test app with the adapter enabled and its own adapter installed.

    ``app_fixture`` includes ``actual_app``'s router, and the ``/api/mcp`` route
    ``create_app`` installed travels with it, pointing at ``actual_app``'s
    adapter whose session manager belongs to the live server's event loop. That
    route is dropped so the adapter installed here is the one that serves.
    ``mcp_session`` runs its session manager, because ``ASGITransport`` runs no
    lifespan and the anyio task group inside must open and close in one task.
    """
    profile_id = api_test_processing_service.service_config.id
    app_fixture.state.processing_services = {profile_id: api_test_processing_service}
    # As the production lifespan does: the mcp-typed interface saves into the
    # adapter's own history partition.
    app_fixture.state.chat_interfaces = {
        "web": app_fixture.state.web_chat_interface,
        "mcp": WebChatInterface(db_engine, interface_type="mcp"),
    }
    _configure_adapter(app_fixture, db_engine, MCPAdapterConfig(enabled=True))
    app_fixture.router.routes[:] = [
        route
        for route in app_fixture.router.routes
        if getattr(route, "name", None) != "mcp_adapter"
    ]
    install_mcp_adapter(app_fixture)
    return app_fixture


@asynccontextmanager
async def mcp_session(app: FastAPI) -> AsyncIterator[ClientSession]:
    """An initialised MCP client session against ``app`` over ASGI."""

    def client_factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            headers=headers,
            timeout=timeout,
            auth=auth,
        )

    adapter = app.state.mcp_adapter
    assert isinstance(adapter, MCPAdapter)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(adapter.run())
        read_stream, write_stream, _get_session_id = await stack.enter_async_context(
            streamablehttp_client(MCP_URL, httpx_client_factory=client_factory)
        )
        session = await stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await session.initialize()
        yield session


def _reply_text(content: list) -> str:
    assert content and isinstance(content[0], TextContent)
    return content[0].text


@pytest.mark.asyncio
async def test_list_tools_advertises_ask_family_assistant(mcp_app: FastAPI) -> None:
    async with mcp_session(mcp_app) as session:
        tools = await session.list_tools()

    tool = next(t for t in tools.tools if t.name == "ask_family_assistant")
    assert set(tool.inputSchema["required"]) == {"question"}
    assert "conversation_id" in tool.inputSchema["properties"]
    assert tool.outputSchema is not None
    assert set(tool.outputSchema["required"]) == {"reply", "conversation_id"}
    assert "conversation_id" in (tool.description or "")


@pytest.mark.asyncio
async def test_question_is_answered_and_persisted_as_mcp(
    mcp_app: FastAPI,
    api_mock_llm_client: RuleBasedMockLLMClient,
    api_db_context: Database,
) -> None:
    api_mock_llm_client.rules.append((
        lambda args: "milk" in str(args.get("messages", [])),
        LLMOutput(content="Milk is on the shopping list."),
    ))

    async with mcp_session(mcp_app) as session:
        result = await session.call_tool(
            "ask_family_assistant", {"question": "Is milk on the shopping list?"}
        )

    assert not result.isError
    assert result.structuredContent is not None
    assert result.structuredContent["reply"] == "Milk is on the shopping list."
    conversation_id = result.structuredContent["conversation_id"]
    assert conversation_id.startswith("mcp-")
    assert "Milk is on the shopping list." in _reply_text(result.content)

    rows = await api_db_context.message_history.get_recent_with_metadata(
        interface_type="mcp", conversation_id=conversation_id
    )
    assert [(row["role"], row["user_id"]) for row in rows] == [
        ("user", "test_user"),
        ("assistant", "test_user"),
    ]
    assert rows[0]["content"] == "Is milk on the shopping list?"


@pytest.mark.asyncio
async def test_conversation_id_continues_the_conversation(
    mcp_app: FastAPI,
    api_mock_llm_client: RuleBasedMockLLMClient,
) -> None:
    def second_turn_sees_first(args: MatcherArgs) -> bool:
        history = str(args.get("messages", []))
        return "follow-up" in history and "first question" in history

    api_mock_llm_client.rules.append((
        second_turn_sees_first,
        LLMOutput(content="Second reply, with the first in view."),
    ))
    api_mock_llm_client.rules.append((
        lambda args: "first question" in str(args.get("messages", [])),
        LLMOutput(content="First reply."),
    ))

    async with mcp_session(mcp_app) as session:
        first = await session.call_tool(
            "ask_family_assistant", {"question": "This is the first question."}
        )
        assert first.structuredContent is not None
        conversation_id = first.structuredContent["conversation_id"]
        second = await session.call_tool(
            "ask_family_assistant",
            {"question": "And a follow-up.", "conversation_id": conversation_id},
        )

    assert not second.isError
    assert second.structuredContent is not None
    assert second.structuredContent["reply"] == "Second reply, with the first in view."
    assert second.structuredContent["conversation_id"] == conversation_id


@pytest.mark.asyncio
async def test_another_users_conversation_is_not_found(
    mcp_app: FastAPI,
    api_mock_llm_client: RuleBasedMockLLMClient,
    api_db_context: Database,
) -> None:
    conversation_id = "mcp-belongs-to-someone-else"
    await api_db_context.message_history.add_message(
        UserMessage(content="Private question."),
        interface_type="mcp",
        conversation_id=conversation_id,
        timestamp=datetime.now(UTC),
        user_id="someone_else",
    )
    api_mock_llm_client.rules.append((
        lambda _args: True,
        LLMOutput(content="Should never be generated."),
    ))

    async with mcp_session(mcp_app) as session:
        result = await session.call_tool(
            "ask_family_assistant",
            {"question": "What did they ask?", "conversation_id": conversation_id},
        )

    assert result.isError
    assert "not found" in _reply_text(result.content).lower()
    assert (
        await api_db_context.message_history.get_conversation_message_count(
            conversation_id
        )
        == 1
    )
    assert api_mock_llm_client.get_calls() == []


@pytest.mark.asyncio
async def test_unknown_configured_profile_is_reported(
    mcp_app: FastAPI,
    api_mock_llm_client: RuleBasedMockLLMClient,
    db_engine: AsyncEngine,
) -> None:
    _configure_adapter(
        mcp_app, db_engine, MCPAdapterConfig(enabled=True, profile_id="no_such_profile")
    )
    api_mock_llm_client.rules.append((
        lambda _args: True,
        LLMOutput(content="Should never be generated."),
    ))

    async with mcp_session(mcp_app) as session:
        result = await session.call_tool(
            "ask_family_assistant", {"question": "Anything?"}
        )

    assert result.isError
    assert "no_such_profile" in _reply_text(result.content)
    assert api_mock_llm_client.get_calls() == []


@pytest.mark.asyncio
async def test_disabled_adapter_is_not_found(
    app_fixture: FastAPI, db_engine: AsyncEngine
) -> None:
    _configure_adapter(app_fixture, db_engine, MCPAdapterConfig(enabled=False))

    async with AsyncClient(
        transport=ASGITransport(app=app_fixture), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_mcp_chat_interface_saves_into_the_mcp_partition(
    db_engine: AsyncEngine,
) -> None:
    """Deferred results delivered through chat_interfaces["mcp"] land where the
    client's next call reads them."""
    interface = WebChatInterface(db_engine, interface_type="mcp")
    db = Database(engine=db_engine)
    await db.message_history.add_message(
        UserMessage(content="approve that"),
        interface_type="mcp",
        conversation_id="mcp-deferred",
        user_id="test_user",
        timestamp=datetime.now(UTC),
    )

    await interface.send_message("mcp-deferred", "Done: the note was saved.")

    history = await db.message_history.get_recent(
        interface_type="mcp", conversation_id="mcp-deferred", limit=5
    )
    assert [m.content for m in history][-1] == "Done: the note was saved."
