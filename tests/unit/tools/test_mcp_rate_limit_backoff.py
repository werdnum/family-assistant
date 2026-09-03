"""Unit tests for generic MCP tool-call rate-limit backoff.

Covers the application-level pacing path in ``MCPToolsProvider.execute_tool``:
a rate-limit-shaped error returned inside an otherwise healthy MCP response
(e.g. an upstream HTTP 429 surfaced as an ``isError`` result) is retried with
short exponential backoff instead of being handed straight back to the calling
LLM, and once retries are exhausted the server enters a fast-fail window.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch

import pytest
from mcp.types import CallToolResult, TextContent

from family_assistant.tools import MCPServerConfig, MCPToolsProvider
from family_assistant.tools import mcp as mcp_module

if TYPE_CHECKING:
    from mcp import ClientSession

    from family_assistant.tools.types import ToolExecutionContext

SERVER_ID = "search-server"
TOOL_NAME = "web_search"
BRAVE_STYLE_ERROR = "Error: Rate limit exceeded"


def _provider(**kwargs: float) -> MCPToolsProvider:
    configs: dict[str, MCPServerConfig] = {
        SERVER_ID: {"transport": "stdio", "command": "echo"}
    }
    return MCPToolsProvider(configs, **kwargs)  # type: ignore[arg-type]


def _wire_session(provider: MCPToolsProvider, session: ClientSession) -> AsyncMock:
    """Attach an already-initialized provider state pointing at ``session``.

    Returns the session's ``call_tool`` mock so tests can assert on its call
    count without narrowing through the ``ClientSession`` cast.
    """
    provider._initialized = True
    provider._tool_map[TOOL_NAME] = SERVER_ID
    provider._sessions[SERVER_ID] = session
    return cast("AsyncMock", session.call_tool)  # type: ignore[redundant-cast]


def _session_returning(result: CallToolResult) -> ClientSession:
    call_tool = AsyncMock(return_value=result)
    return cast("ClientSession", SimpleNamespace(call_tool=call_tool))


def _session_raising(error: Exception) -> ClientSession:
    call_tool = AsyncMock(side_effect=error)
    return cast("ClientSession", SimpleNamespace(call_tool=call_tool))


def _session_with_outcomes(*outcomes: CallToolResult | Exception) -> ClientSession:
    """A session whose ``call_tool`` replays ``outcomes`` in order."""
    call_tool = AsyncMock(side_effect=list(outcomes))
    return cast("ClientSession", SimpleNamespace(call_tool=call_tool))


def _error_result(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=True)


def _ok_result(text: str = "search results here") -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=False)


async def _execute(provider: MCPToolsProvider) -> str:
    context = cast("ToolExecutionContext", SimpleNamespace())
    return await provider.execute_tool(name=TOOL_NAME, arguments={}, context=context)


class TestRateLimitClassifier:
    """The generic error-text classifier (not tied to any server or message)."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # Client-side limiter text, as emitted by the official Brave server.
            ("Error: Rate limit exceeded", True),
            # Raw upstream 429s, several common phrasings.
            ("HTTP 429 Too Many Requests", True),
            ("Request failed with status 429", True),
            ("Brave API error: 429", True),
            # Quota / throttling language from other providers.
            ("quota exceeded for project", True),
            ("Throttled: slow down", True),
            ("you have exceeded your monthly quota", True),
            # Case insensitivity.
            ("RATE LIMIT EXCEEDED", True),
            # A bare 429 with no error context must not match (could be data).
            ("found 429 matching records", False),
            ("page 429 of results", False),
            # Unrelated failures and normal payloads must not match.
            ("Connection closed by peer", False),
            ("invalid arguments: expected string", False),
            ("Here are today's headlines", False),
        ],
    )
    def test_classification(self, text: str, *, expected: bool) -> None:
        assert mcp_module._is_rate_limit_error(text) is expected


@pytest.mark.asyncio
@patch("asyncio.sleep", new_callable=AsyncMock)
async def test_rate_limited_result_is_retried_then_succeeds(
    mock_sleep: AsyncMock,
) -> None:
    """Two paced retries bridge a short rate-limit window; success clears state."""
    provider = _provider(rate_limit_backoff_base_seconds=0.5)
    session = _session_with_outcomes(
        _error_result(BRAVE_STYLE_ERROR),
        _error_result(BRAVE_STYLE_ERROR),
        _ok_result("late results"),
    )
    call_tool = _wire_session(provider, session)

    result = await _execute(provider)

    assert result == "late results"
    assert call_tool.await_count == 3  # initial call + 2 retries
    assert mock_sleep.await_count == 2
    # The clean result must clear the penalty state.
    backoff = provider._rate_limit_backoff[SERVER_ID]
    assert backoff.attempts == 0
    assert backoff.blocked_until == 0.0


@pytest.mark.asyncio
@patch("asyncio.sleep", new_callable=AsyncMock)
async def test_exhausted_retries_return_guidance_and_block_new_calls(
    mock_sleep: AsyncMock,
) -> None:
    """After retries are spent the LLM gets guidance and the server fails fast."""
    provider = _provider(rate_limit_backoff_base_seconds=0.5, rate_limit_max_retries=1)
    call_tool = _wire_session(
        provider, _session_returning(_error_result(BRAVE_STYLE_ERROR))
    )

    first = await _execute(provider)
    assert first.startswith("Rate limited:")
    assert "web_search" in first
    assert call_tool.await_count == 2  # initial call + 1 retry
    assert mock_sleep.await_count == 1

    # While blocked, a new call fails fast without touching the server.
    second = await _execute(provider)
    assert second.startswith("Rate limited:")
    assert call_tool.await_count == 2  # unchanged

    backoff = provider._rate_limit_backoff[SERVER_ID]
    assert backoff.attempts == 2
    assert backoff.blocked_until > 0.0


@pytest.mark.asyncio
async def test_calls_resume_after_block_window_elapses() -> None:
    """Once the penalty window has passed, tool calls go through again."""
    provider = _provider(rate_limit_backoff_base_seconds=0.5)
    call_tool = _wire_session(provider, _session_returning(_ok_result()))
    provider._rate_limit_backoff[SERVER_ID] = mcp_module._RateLimitBackoff(
        attempts=3, blocked_until=time.monotonic() - 1.0
    )

    result = await _execute(provider)

    assert result == "search results here"
    assert call_tool.await_count == 1
    assert provider._rate_limit_backoff[SERVER_ID].attempts == 0


@pytest.mark.asyncio
@patch("asyncio.sleep", new_callable=AsyncMock)
async def test_non_rate_limit_errors_are_not_retried(mock_sleep: AsyncMock) -> None:
    """Unrelated error results go straight back to the LLM, with no pacing."""
    provider = _provider()
    call_tool = _wire_session(
        provider, _session_returning(_error_result("No results found for query"))
    )

    result = await _execute(provider)

    assert result == f"Error executing tool '{TOOL_NAME}': No results found for query"
    assert call_tool.await_count == 1
    assert mock_sleep.await_count == 0
    assert provider._rate_limit_backoff[SERVER_ID].attempts == 0


@pytest.mark.asyncio
@patch("asyncio.sleep", new_callable=AsyncMock)
async def test_transport_level_429_is_paced_without_spending_reconnect_budget(
    mock_sleep: AsyncMock,
) -> None:
    """A 429 raised as a transport exception paces but never reconnects."""
    provider = _provider(rate_limit_backoff_base_seconds=0.5)
    session = _session_with_outcomes(
        RuntimeError("HTTP 429 Too Many Requests"),
        _ok_result("eventual success"),
    )
    call_tool = _wire_session(provider, session)

    result = await _execute(provider)

    assert result == "eventual success"
    assert call_tool.await_count == 2
    assert mock_sleep.await_count == 1


@pytest.mark.asyncio
@patch("asyncio.sleep", new_callable=AsyncMock)
async def test_per_server_isolation(mock_sleep: AsyncMock) -> None:
    """One server's rate-limit penalty must not affect another server's calls."""
    configs: dict[str, MCPServerConfig] = {
        "a": {"transport": "stdio", "command": "echo"},
        "b": {"transport": "stdio", "command": "echo"},
    }
    provider = MCPToolsProvider(configs)  # type: ignore[arg-type]
    provider._initialized = True
    session_a = _session_returning(_error_result(BRAVE_STYLE_ERROR))
    session_b = _session_returning(_ok_result("server b results"))
    provider._tool_map["tool_a"] = "a"
    provider._tool_map["tool_b"] = "b"
    provider._sessions["a"] = session_a
    provider._sessions["b"] = session_b

    context = cast("ToolExecutionContext", SimpleNamespace())
    blocked = await provider.execute_tool(name="tool_a", arguments={}, context=context)
    assert blocked.startswith("Rate limited:")
    a_backoff = provider._rate_limit_backoff["a"]
    assert a_backoff.attempts == provider._rate_limit_max_retries + 1
    assert a_backoff.blocked_until > 0.0

    clean = await provider.execute_tool(name="tool_b", arguments={}, context=context)
    assert clean == "server b results"
    assert mock_sleep.await_count == provider._rate_limit_max_retries
    assert provider._rate_limit_backoff["b"].attempts == 0
