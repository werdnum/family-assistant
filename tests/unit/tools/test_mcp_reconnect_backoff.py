"""Unit tests for MCP reconnect pacing (truncated exponential backoff with jitter)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest

from family_assistant.tools import MCPServerConfig, MCPToolsProvider
from family_assistant.tools.mcp import (
    DEFAULT_RECONNECT_BACKOFF_MAX_SECONDS,
    MCP_SERVER_STATUS_CONNECTED,
    MCP_SERVER_STATUS_FAILED,
    reconnect_backoff_delay,
)

if TYPE_CHECKING:
    from mcp import ClientSession

    from family_assistant.tools.metadata import ToolDescriptor
    from family_assistant.tools.types import ToolDefinition

SERVER_ID = "flaky-server"


def _provider(**kwargs: float) -> MCPToolsProvider:
    configs: dict[str, MCPServerConfig] = {
        SERVER_ID: {"transport": "stdio", "command": "echo"}
    }
    return MCPToolsProvider(configs, **kwargs)  # type: ignore[arg-type]


def _make_session(*, healthy: bool = True) -> ClientSession:
    """A stand-in session whose health check passes or reports a dead transport."""
    list_tools = AsyncMock(
        side_effect=None if healthy else ConnectionError("connection closed by peer")
    )
    return cast("ClientSession", SimpleNamespace(list_tools=list_tools))


def _connect_stub(provider: MCPToolsProvider, *, succeed: bool) -> AsyncMock:
    """Replace ``_connect_and_discover_mcp`` with a stub of a fixed outcome."""

    async def fake_connect(
        server_id: str, server_conf: MCPServerConfig
    ) -> tuple[ClientSession | None, list[ToolDefinition], list[ToolDescriptor], dict]:
        del server_conf
        if not succeed:
            provider._server_statuses[server_id] = MCP_SERVER_STATUS_FAILED
            return None, [], [], {}
        provider._server_statuses[server_id] = MCP_SERVER_STATUS_CONNECTED
        return _make_session(), [], [], {}

    stub = AsyncMock(side_effect=fake_connect)
    provider._connect_and_discover_mcp = stub  # type: ignore[method-assign]
    provider._close_server_connections = AsyncMock()  # type: ignore[method-assign]
    return stub


def test_backoff_delay_doubles_and_is_capped() -> None:
    """Each attempt doubles the window until the cap, and jitter halves the floor."""
    delays = [
        reconnect_backoff_delay(attempts, base_seconds=30.0, max_seconds=1800.0)
        for attempts in range(1, 12)
    ]

    for attempts, delay in enumerate(delays, start=1):
        ceiling = min(1800.0, 30.0 * 2 ** (attempts - 1))
        assert ceiling / 2 <= delay <= ceiling

    assert delays[-1] >= 900.0


def test_backoff_delay_is_zero_before_any_attempt() -> None:
    """The first reconnect after a drop is not delayed."""
    assert reconnect_backoff_delay(0, base_seconds=30.0, max_seconds=1800.0) == 0.0


def test_backoff_delay_does_not_overflow_after_many_attempts() -> None:
    """A long-lived process keeps returning the cap rather than overflowing."""
    delay = reconnect_backoff_delay(
        5000, base_seconds=30.0, max_seconds=DEFAULT_RECONNECT_BACKOFF_MAX_SECONDS
    )

    assert (
        DEFAULT_RECONNECT_BACKOFF_MAX_SECONDS / 2
        <= delay
        <= DEFAULT_RECONNECT_BACKOFF_MAX_SECONDS
    )


@pytest.mark.asyncio
async def test_failed_server_is_not_retried_until_its_window_elapses() -> None:
    """A server that stays down is retried once, then left alone while backing off."""
    provider = _provider(reconnect_backoff_base_seconds=600.0)
    connect = _connect_stub(provider, succeed=False)
    provider._server_statuses[SERVER_ID] = MCP_SERVER_STATUS_FAILED

    await provider._retry_disconnected_servers([SERVER_ID])
    await provider._retry_disconnected_servers([SERVER_ID])
    await provider._retry_disconnected_servers([SERVER_ID])

    assert connect.await_count == 1
    assert provider._reconnect_backoff[SERVER_ID].attempts == 1


@pytest.mark.asyncio
async def test_retry_resumes_once_the_backoff_window_expires() -> None:
    """Once the window has passed the server is retried again, with a longer window."""
    provider = _provider(reconnect_backoff_base_seconds=600.0)
    connect = _connect_stub(provider, succeed=False)
    provider._server_statuses[SERVER_ID] = MCP_SERVER_STATUS_FAILED
    await provider._retry_disconnected_servers([SERVER_ID])
    first_window = provider._reconnect_backoff[SERVER_ID].next_attempt_at

    provider._reconnect_backoff[SERVER_ID].next_attempt_at = 0.0
    await provider._retry_disconnected_servers([SERVER_ID])

    assert connect.await_count == 2
    assert provider._reconnect_backoff[SERVER_ID].attempts == 2
    assert provider._reconnect_backoff[SERVER_ID].next_attempt_at > first_window


@pytest.mark.asyncio
async def test_dropped_session_is_reconnected_immediately() -> None:
    """A server that was healthy until now gets its first retry without delay."""
    provider = _provider(reconnect_backoff_base_seconds=600.0)
    connect = _connect_stub(provider, succeed=True)
    provider._sessions[SERVER_ID] = _make_session(healthy=False)

    await provider._run_health_checks()

    assert connect.await_count == 1
    assert provider._server_statuses[SERVER_ID] == MCP_SERVER_STATUS_CONNECTED


@pytest.mark.asyncio
async def test_flapping_server_backs_off_despite_successful_reconnects() -> None:
    """Reconnects that succeed and then drop still extend the window.

    Otherwise a server that accepts a connection and immediately closes it
    would be respawned on every health check cycle, forever.
    """
    provider = _provider(reconnect_backoff_base_seconds=600.0)
    connect = _connect_stub(provider, succeed=True)
    provider._sessions[SERVER_ID] = _make_session(healthy=False)
    await provider._run_health_checks()

    provider._sessions[SERVER_ID] = _make_session(healthy=False)
    await provider._run_health_checks()

    assert connect.await_count == 1
    assert provider._reconnect_backoff[SERVER_ID].attempts == 1


@pytest.mark.asyncio
async def test_passing_health_check_clears_the_backoff() -> None:
    """Surviving a full interval is what proves the server is usable again."""
    provider = _provider(reconnect_backoff_base_seconds=600.0)
    _connect_stub(provider, succeed=False)
    provider._server_statuses[SERVER_ID] = MCP_SERVER_STATUS_FAILED
    await provider._retry_disconnected_servers([SERVER_ID])
    provider._sessions[SERVER_ID] = _make_session()

    await provider._run_health_checks()

    assert provider._reconnect_backoff[SERVER_ID].attempts == 0
    assert provider._reconnect_backoff[SERVER_ID].next_attempt_at == 0.0


@pytest.mark.asyncio
async def test_manual_reconnect_ignores_and_clears_the_backoff_window() -> None:
    """An operator asking for a reconnect knows something the schedule doesn't."""
    provider = _provider(reconnect_backoff_base_seconds=600.0)
    _connect_stub(provider, succeed=False)
    provider._server_statuses[SERVER_ID] = MCP_SERVER_STATUS_FAILED
    await provider._retry_disconnected_servers([SERVER_ID])
    recovered_connect = _connect_stub(provider, succeed=True)

    reconnected = await provider.reconnect_server(SERVER_ID)

    assert reconnected is True
    assert recovered_connect.await_count == 1
    assert provider._reconnect_backoff[SERVER_ID].attempts == 0
    assert provider._reconnect_backoff[SERVER_ID].next_attempt_at == 0.0


@pytest.mark.asyncio
async def test_server_status_reports_pending_backoff() -> None:
    """The engineer profile can see why a failed server hasn't been retried yet."""
    provider = _provider(reconnect_backoff_base_seconds=600.0)
    _connect_stub(provider, succeed=False)
    provider._server_statuses[SERVER_ID] = MCP_SERVER_STATUS_FAILED

    await provider._retry_disconnected_servers([SERVER_ID])

    status = provider.get_server_statuses()[SERVER_ID]
    assert status["reconnect_attempts"] == 1
    next_reconnect = status["next_reconnect_in_seconds"]
    assert next_reconnect is not None
    assert 0.0 < next_reconnect <= 600.0


@pytest.mark.asyncio
async def test_server_status_omits_backoff_for_healthy_servers() -> None:
    """A server that has never needed retrying reports no pending reconnect."""
    provider = _provider()
    provider._server_statuses[SERVER_ID] = MCP_SERVER_STATUS_CONNECTED

    status = provider.get_server_statuses()[SERVER_ID]

    assert status["reconnect_attempts"] == 0
    assert status["next_reconnect_in_seconds"] is None
