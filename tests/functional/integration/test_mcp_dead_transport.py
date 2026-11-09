"""A server that rejects a notification kills the real streamable HTTP transport.

The SDK's streamable HTTP client POSTs a notification inline in its writer task,
so a failed POST ends that task and leaves the session's write stream with no
reader: the next request raises an empty ``anyio.BrokenResourceError``. These
tests drive that through the real client on loopback.
"""

from __future__ import annotations

import asyncio
import logging
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

from family_assistant.tools.mcp import (
    MCP_SERVER_STATUS_CONNECTED,
    MCP_SERVER_STATUS_FAILED,
    MCPToolsProvider,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.requests import Request

    from family_assistant.tools.types import MCPServerConfig, ToolExecutionContext

SERVER_ID = "flaky"


@dataclass
class Backend:
    url: str = ""
    reject_notifications: bool = False
    rejected: asyncio.Event = field(default_factory=asyncio.Event)

    async def handle(self, request: Request) -> Response:
        if request.method != "POST":
            return Response(status_code=405)
        rpc = await request.json()
        if "id" not in rpc:
            if self.reject_notifications:
                self.rejected.set()
                return Response(status_code=503)
            return Response(status_code=202)
        result: dict[str, object] = {}
        if rpc["method"] == "initialize":
            result = {
                "protocolVersion": rpc["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "flaky", "version": "1"},
            }
        elif rpc["method"] == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            }
        elif rpc["method"] == "tools/call":
            result = {"content": [{"type": "text", "text": "ok"}]}
        return JSONResponse({"jsonrpc": "2.0", "id": rpc["id"], "result": result})


@pytest_asyncio.fixture
async def backend() -> AsyncIterator[Backend]:
    backend = Backend()
    app = Starlette(
        routes=[Route("/mcp", backend.handle, methods=["GET", "POST", "DELETE"])]
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


def _provider(backend: Backend) -> MCPToolsProvider:
    config: MCPServerConfig = {
        "transport": "streamable_http",
        "url": backend.url + "/mcp",
    }
    return MCPToolsProvider({SERVER_ID: config}, health_check_interval_seconds=3600)


def _context() -> ToolExecutionContext:
    return cast("ToolExecutionContext", SimpleNamespace(user_id=None))


async def _kill_transport(provider: MCPToolsProvider, backend: Backend) -> None:
    """Have the server reject one client notification, ending the writer task."""
    backend.reject_notifications = True
    await provider._sessions[SERVER_ID].send_roots_list_changed()
    # The send returns once the writer task takes the message, before its POST
    # reaches the server.
    await asyncio.wait_for(backend.rejected.wait(), timeout=10)
    backend.reject_notifications = False


@pytest.mark.asyncio
async def test_rejected_initialized_notification_is_a_retried_warning(
    backend: Backend, caplog: pytest.LogCaptureFixture
) -> None:
    provider = _provider(backend)
    backend.reject_notifications = True
    caplog.set_level(logging.WARNING, logger="family_assistant.tools.mcp")
    try:
        await provider.initialize()

        assert provider._server_statuses[SERVER_ID] == MCP_SERVER_STATUS_FAILED
        provider_records = [
            r for r in caplog.records if r.name == "family_assistant.tools.mcp"
        ]
        assert any(
            "lost its transport during connection/discovery" in r.getMessage()
            and r.levelno == logging.WARNING
            for r in provider_records
        )
        assert not [r for r in provider_records if r.levelno >= logging.ERROR]

        backend.reject_notifications = False
        await provider._retry_disconnected_servers([SERVER_ID])

        assert provider._server_statuses[SERVER_ID] == MCP_SERVER_STATUS_CONNECTED
        assert await provider.execute_tool("echo", {}, _context()) == "ok"
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_health_check_reconnects_a_session_whose_writer_died(
    backend: Backend,
) -> None:
    provider = _provider(backend)
    await provider.initialize()
    try:
        dead = provider._sessions[SERVER_ID]
        await _kill_transport(provider, backend)

        await provider._run_health_checks()

        assert provider._sessions[SERVER_ID] is not dead
        assert provider._server_statuses[SERVER_ID] == MCP_SERVER_STATUS_CONNECTED
        assert await provider.execute_tool("echo", {}, _context()) == "ok"
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_tool_call_reconnects_a_session_whose_writer_died(
    backend: Backend,
) -> None:
    provider = _provider(backend)
    await provider.initialize()
    try:
        await _kill_transport(provider, backend)

        assert await provider.execute_tool("echo", {}, _context()) == "ok"
    finally:
        await provider.close()
