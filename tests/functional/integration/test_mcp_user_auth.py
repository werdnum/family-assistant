"""Exercise user-scoped connections with the real MCP HTTP client on loopback."""

from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
import pytest_asyncio
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from family_assistant.tools.mcp import MCPToolsProvider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.requests import Request

    from family_assistant.tools.types import MCPServerConfig, ToolExecutionContext


@dataclass
class Backend:
    url: str = ""
    revoked: set[str] = field(default_factory=set)
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def handle(self, request: Request) -> Response:
        user = {"Bearer alex-token": "alex", "Bearer sam-token": "sam"}.get(
            request.headers.get("authorization", "")
        )
        if user is None or user in self.revoked:
            return Response(status_code=401)
        if request.url.path == "/api/me":
            return JSONResponse({
                "user": {"id": user},
                "agent": "family-assistant",
                "can_write": True,
            })
        if request.method != "POST":
            return Response(status_code=405)
        rpc = await request.json()
        method = rpc["method"]
        if "id" not in rpc:
            return Response(status_code=202)
        result: dict[str, object] = {}
        if method == "initialize":
            result = {
                "protocolVersion": rpc["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test", "version": "1"},
                "instructions": f"You act for {user}.",
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "record",
                        "description": "Record an action",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            }
        elif method == "tools/call":
            self.calls.append((user, rpc["params"]["name"]))
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"user": user, "agent": "family-assistant"}),
                    }
                ]
            }
        return JSONResponse({"jsonrpc": "2.0", "id": rpc["id"], "result": result})


@pytest_asyncio.fixture
async def backend() -> AsyncIterator[Backend]:
    backend = Backend()
    app = Starlette(
        routes=[
            Route("/api/me", backend.handle),
            Route("/mcp", backend.handle, methods=["GET", "POST", "DELETE"]),
        ]
    )
    ready = asyncio.Event()

    class Server(uvicorn.Server):
        async def startup(self, sockets: list[socket.socket] | None = None) -> None:
            await super().startup(sockets)
            ready.set()

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        backend.url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        await asyncio.wait_for(ready.wait(), timeout=10)
        try:
            yield backend
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=10)


def context(user_id: str | None) -> ToolExecutionContext:
    return cast("ToolExecutionContext", SimpleNamespace(user_id=user_id))


@pytest_asyncio.fixture
async def provider(
    backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[MCPToolsProvider]:
    monkeypatch.setenv("TUIT_ALEX", "alex-token")
    monkeypatch.setenv("TUIT_SAM", "sam-token")
    config: MCPServerConfig = {
        "transport": "streamable_http",
        "tool_name_prefix": "tuit_",
        "url": backend.url + "/mcp",
        "user_auth": {
            "call_timeout_seconds": 2,
            "users": {
                "alex@example.com": {
                    "token_env": "TUIT_ALEX",
                    "expected_user_id": "alex",
                },
                "sam@example.com": {"token_env": "TUIT_SAM", "expected_user_id": "sam"},
            },
            "identity_check": {"expected_agent": "family-assistant"},
        },
    }
    provider = MCPToolsProvider({"tuit": config}, health_check_interval_seconds=3600)
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_concurrent_calls_use_context_identity(
    provider: MCPToolsProvider, backend: Backend
) -> None:
    results = await asyncio.gather(
        provider.execute_tool(
            "tuit_record", {"user_id": "sam"}, context("alex@example.com")
        ),
        provider.execute_tool(
            "tuit_record", {"user_id": "alex"}, context("sam@example.com")
        ),
    )
    assert [json.loads(result)["user"] for result in results] == ["alex", "sam"]
    assert sorted(backend.calls) == [("alex", "record"), ("sam", "record")]
    assert len(await provider.get_tool_definitions()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [None, "unknown"])
async def test_missing_user_is_actionable(
    provider: MCPToolsProvider, backend: Backend, user_id: str | None
) -> None:
    result = await provider.execute_tool("tuit_record", {}, context(user_id))
    assert result.startswith("Error:")
    assert backend.calls == []


@pytest.mark.asyncio
async def test_reconnect_retains_both_identities(
    provider: MCPToolsProvider, backend: Backend
) -> None:
    await provider.reconnect_server("tuit")
    results = await asyncio.gather(
        *(
            provider.execute_tool("tuit_record", {}, context(user + "@example.com"))
            for user in ("alex", "sam")
        )
    )
    assert [json.loads(result)["user"] for result in results] == ["alex", "sam"]


@pytest.mark.asyncio
async def test_swapped_token_refused_on_reconnect(
    provider: MCPToolsProvider, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TUIT_ALEX", "sam-token")
    await provider.reconnect_server("tuit")
    result = await provider.execute_tool("tuit_record", {}, context("alex@example.com"))
    assert result.startswith("Error:")
    assert backend.calls == []
    assert provider.get_server_statuses()["tuit"].get("users") == {
        "alex@example.com": "failed",
        "sam@example.com": "connected",
    }


@pytest.mark.asyncio
async def test_revocation_does_not_break_other_user(
    provider: MCPToolsProvider, backend: Backend
) -> None:
    backend.revoked.add("alex")
    results = await asyncio.gather(
        *(
            provider.execute_tool("tuit_record", {}, context(user + "@example.com"))
            for user in ("alex", "sam")
        )
    )
    assert results[0].startswith("Error")
    assert json.loads(results[1])["user"] == "sam"
    assert backend.calls == [("sam", "record")]


@pytest.mark.asyncio
async def test_missing_token_never_connects_anonymously(
    provider: MCPToolsProvider, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TUIT_ALEX")
    await provider.reconnect_server("tuit")
    result = await provider.execute_tool("tuit_record", {}, context("alex@example.com"))
    assert result.startswith("Error:")
    assert backend.calls == []


@pytest.mark.asyncio
async def test_initialization_instructions_stay_per_user(
    provider: MCPToolsProvider,
) -> None:
    connections = provider._user_connections["tuit"].connections
    assert (
        connections["alex@example.com"].provider._session_instructions["tuit"]
        == "You act for alex."
    )
    assert (
        connections["sam@example.com"].provider._session_instructions["tuit"]
        == "You act for sam."
    )
    assert provider._session_instructions == {}


@pytest.mark.asyncio
async def test_health_recovers_after_transport_closes(
    provider: MCPToolsProvider, backend: Backend
) -> None:
    backend.revoked.add("alex")
    await provider.execute_tool("tuit_record", {}, context("alex@example.com"))
    backend.revoked.clear()

    await provider._run_health_checks()
    result = await provider.execute_tool("tuit_record", {}, context("alex@example.com"))

    assert json.loads(result)["user"] == "alex"


@pytest.mark.asyncio
async def test_reconnect_and_close_keep_sdk_contexts_in_their_owning_task(
    provider: MCPToolsProvider, caplog: pytest.LogCaptureFixture
) -> None:
    await provider.reconnect_server("tuit")
    await provider.close()

    assert "cancel scope" not in caplog.text
