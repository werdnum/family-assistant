"""Unit tests for MCP connection ownership and health check loop survival.

A shared server's transport is held open by a dedicated task. A transport that
dies cancels only that task, and a cancellation that escapes a reconnect costs
one health check pass rather than stopping the loop.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import anyio
import pytest
from mcp.types import ListToolsResult

from family_assistant.tools.mcp import (
    CONSECUTIVE_PING_TIMEOUTS_BEFORE_RECONNECT,
    MCP_SERVER_STATUS_CONNECTED,
    MCP_SERVER_STATUS_FAILED,
    MCPToolsProvider,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mcp import ClientSession

    from family_assistant.tools.metadata import ToolDescriptor
    from family_assistant.tools.types import MCPServerConfig, ToolDefinition

SERVER_ID = "code-execution"
WAIT_SECONDS = 5.0

type DiscoveryResult = tuple[
    ClientSession | None, list[ToolDefinition], list[ToolDescriptor], dict[str, str]
]


def _provider(
    *,
    health_check_interval_seconds: int = 30,
    reconnect_backoff_base_seconds: float = 30.0,
) -> MCPToolsProvider:
    configs: dict[str, MCPServerConfig] = {
        SERVER_ID: {"transport": "stdio", "command": "echo"}
    }
    return MCPToolsProvider(
        configs,
        health_check_interval_seconds=health_check_interval_seconds,
        reconnect_backoff_base_seconds=reconnect_backoff_base_seconds,
    )


def _session(*, ping_error: BaseException | None = None) -> ClientSession:
    return cast(
        "ClientSession",
        SimpleNamespace(
            send_ping=AsyncMock(return_value=None, side_effect=ping_error),
            list_tools=AsyncMock(return_value=ListToolsResult(tools=[])),
        ),
    )


class _FakeTransport:
    """A transport whose background reader can fail, like the SDK's SSE client.

    Its task group's cancel scope belongs to whichever task entered it, so a
    reader failure cancels that task.
    """

    def __init__(self) -> None:
        self.reader_fails = anyio.Event()
        self.exited = False

    @contextlib.asynccontextmanager
    async def open(self) -> AsyncIterator[None]:
        async def reader() -> None:
            await self.reader_fails.wait()
            raise ConnectionError("sse_reader: peer closed connection")

        try:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(reader)
                yield
                task_group.cancel_scope.cancel()
        finally:
            self.exited = True


def _open_with(
    monkeypatch: pytest.MonkeyPatch,
    provider: MCPToolsProvider,
    transport: _FakeTransport,
    session: ClientSession,
) -> None:
    """Make the provider open ``transport`` instead of a real server."""

    async def fake_open(
        server_id: str,
        server_conf: MCPServerConfig,
        exit_stack: contextlib.AsyncExitStack,
    ) -> DiscoveryResult:
        del server_conf
        await exit_stack.enter_async_context(transport.open())
        provider._server_statuses[server_id] = MCP_SERVER_STATUS_CONNECTED
        return session, [], [], {}

    monkeypatch.setattr(provider, "_open_server_connection", fake_open)


async def _connect(provider: MCPToolsProvider) -> ClientSession:
    session, _, _, _ = await provider._connect_and_discover_mcp(
        SERVER_ID, provider.server_configs[SERVER_ID]
    )
    assert session is not None
    provider._sessions[SERVER_ID] = session
    return session


@pytest.mark.asyncio
async def test_dying_transport_cancels_only_its_holder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reader failure that stopped the health loop now ends only the holder."""
    provider = _provider()
    transport = _FakeTransport()
    _open_with(monkeypatch, provider, transport, _session())
    await _connect(provider)
    holder = provider._connections[SERVER_ID].task

    transport.reader_fails.set()
    await asyncio.wait({holder}, timeout=WAIT_SECONDS)

    assert holder.done()
    assert transport.exited
    current = asyncio.current_task()
    assert current is not None
    assert current.cancelling() == 0


@pytest.mark.asyncio
async def test_health_check_reconnects_a_server_whose_holder_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()
    transport = _FakeTransport()
    _open_with(monkeypatch, provider, transport, _session())
    await _connect(provider)
    holder = provider._connections[SERVER_ID].task
    transport.reader_fails.set()
    await asyncio.wait({holder}, timeout=WAIT_SECONDS)

    replacement = _session()
    _open_with(monkeypatch, provider, _FakeTransport(), replacement)
    await provider._run_health_checks()

    assert provider._sessions[SERVER_ID] is replacement
    assert provider._server_statuses[SERVER_ID] == MCP_SERVER_STATUS_CONNECTED
    await provider.close()


@pytest.mark.asyncio
async def test_closing_from_another_task_unwinds_in_the_holder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teardown from the health loop's task no longer exits a foreign cancel scope."""
    provider = _provider()
    transport = _FakeTransport()
    _open_with(monkeypatch, provider, transport, _session())
    await asyncio.create_task(_connect(provider))
    holder = provider._connections[SERVER_ID].task

    await provider._close_server_connections(SERVER_ID)

    assert holder.done()
    assert not holder.cancelled()
    assert holder.exception() is None
    assert transport.exited
    assert SERVER_ID not in provider._connections
    assert SERVER_ID not in provider._sessions


@pytest.mark.asyncio
async def test_connect_reports_failure_when_server_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()
    provider._initialization_timeout_seconds = 0
    hung = asyncio.Event()

    async def hanging_open(
        server_id: str,
        server_conf: MCPServerConfig,
        exit_stack: contextlib.AsyncExitStack,
    ) -> DiscoveryResult:
        del server_id, server_conf, exit_stack
        await hung.wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(provider, "_open_server_connection", hanging_open)

    result = await provider._connect_and_discover_mcp(
        SERVER_ID, provider.server_configs[SERVER_ID]
    )

    assert result[0] is None
    assert provider._server_statuses[SERVER_ID] == MCP_SERVER_STATUS_FAILED
    assert SERVER_ID not in provider._connections


@pytest.mark.asyncio
async def test_failed_discovery_closes_what_it_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport opened before discovery failed is closed before connect returns."""
    provider = _provider()
    transport = _FakeTransport()

    async def open_then_fail(
        server_id: str,
        server_conf: MCPServerConfig,
        exit_stack: contextlib.AsyncExitStack,
    ) -> DiscoveryResult:
        del server_conf
        await exit_stack.enter_async_context(transport.open())
        provider._server_statuses[server_id] = MCP_SERVER_STATUS_FAILED
        return None, [], [], {}

    monkeypatch.setattr(provider, "_open_server_connection", open_then_fail)

    result = await provider._connect_and_discover_mcp(
        SERVER_ID, provider.server_configs[SERVER_ID]
    )

    assert result[0] is None
    assert transport.exited
    assert SERVER_ID not in provider._connections


@pytest.mark.asyncio
async def test_health_loop_survives_a_cancellation_escaping_a_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sunday's incident: a reconnect raised CancelledError and the loop exited."""
    provider = _provider(
        health_check_interval_seconds=0, reconnect_backoff_base_seconds=0.0
    )
    provider._initialized = True
    provider._server_statuses[SERVER_ID] = MCP_SERVER_STATUS_FAILED
    replacement = _session()
    reconnected = asyncio.Event()
    calls = 0

    async def connect(server_id: str, server_conf: MCPServerConfig) -> DiscoveryResult:
        del server_conf
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.CancelledError
        provider._server_statuses[server_id] = MCP_SERVER_STATUS_CONNECTED
        reconnected.set()
        return replacement, [], [], {}

    monkeypatch.setattr(provider, "_connect_and_discover_mcp", connect)
    provider._health_check_task = asyncio.create_task(provider._health_check_loop())

    await asyncio.wait_for(reconnected.wait(), timeout=WAIT_SECONDS)

    assert not provider._health_check_task.done()
    assert provider._sessions[SERVER_ID] is replacement
    await provider.close()
    assert provider._health_check_task.done()


@pytest.mark.asyncio
async def test_health_loop_survives_an_error_in_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(health_check_interval_seconds=0)
    second_pass = asyncio.Event()
    passes = 0

    async def failing_then_ok() -> None:
        nonlocal passes
        passes += 1
        if passes == 1:
            raise RuntimeError("boom")
        second_pass.set()

    monkeypatch.setattr(provider, "_run_health_check_pass", failing_then_ok)
    provider._health_check_task = asyncio.create_task(provider._health_check_loop())

    await asyncio.wait_for(second_pass.wait(), timeout=WAIT_SECONDS)

    assert not provider._health_check_task.done()
    await provider.close()


@pytest.mark.asyncio
async def test_repeated_ping_timeouts_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(reconnect_backoff_base_seconds=0.0)
    provider._initialized = True
    provider._server_statuses[SERVER_ID] = MCP_SERVER_STATUS_CONNECTED
    unresponsive = _session(ping_error=TimeoutError())
    provider._sessions[SERVER_ID] = unresponsive
    replacement = _session()
    connect = AsyncMock(return_value=(replacement, [], [], {}))
    monkeypatch.setattr(provider, "_connect_and_discover_mcp", connect)

    for _ in range(CONSECUTIVE_PING_TIMEOUTS_BEFORE_RECONNECT - 1):
        await provider._run_health_checks()
        assert provider._sessions[SERVER_ID] is unresponsive
    await provider._run_health_checks()

    assert connect.await_count == 1
    assert provider._sessions[SERVER_ID] is replacement


@pytest.mark.asyncio
async def test_a_passing_ping_resets_the_timeout_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()
    provider._initialized = True
    provider._server_statuses[SERVER_ID] = MCP_SERVER_STATUS_CONNECTED
    connect = AsyncMock(return_value=(_session(), [], [], {}))
    monkeypatch.setattr(provider, "_connect_and_discover_mcp", connect)

    slow = _session(ping_error=TimeoutError())
    provider._sessions[SERVER_ID] = slow
    for _ in range(CONSECUTIVE_PING_TIMEOUTS_BEFORE_RECONNECT - 1):
        await provider._run_health_checks()
    cast("AsyncMock", slow.send_ping).side_effect = None
    await provider._run_health_checks()
    cast("AsyncMock", slow.send_ping).side_effect = TimeoutError()
    await provider._run_health_checks()

    assert connect.await_count == 0
    assert provider._sessions[SERVER_ID] is slow
