"""MCP tool calls that never get an answer are abandoned after the configured timeout."""

from __future__ import annotations

from types import SimpleNamespace
from typing import NoReturn, cast

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.exceptions import McpError
from mcp.shared.message import SessionMessage
from mcp.types import INVALID_PARAMS, ErrorData

from family_assistant.tools.mcp import MCPToolsProvider


@pytest.mark.parametrize(
    ("outbound_buffer", "requests_sent"),
    [
        pytest.param(10, 1, id="request sent, no response"),
        pytest.param(0, 0, id="request stuck behind a stalled writer"),
    ],
)
@pytest.mark.asyncio
async def test_a_call_that_never_finishes_times_out_without_a_retry(
    outbound_buffer: int, requests_sent: int
) -> None:
    to_server_send, to_server_receive = anyio.create_memory_object_stream[
        SessionMessage
    ](outbound_buffer)
    to_client_send, to_client_receive = anyio.create_memory_object_stream[
        SessionMessage | Exception
    ](10)
    provider = MCPToolsProvider(
        {"server": {"command": "unused"}}, tool_call_timeout_seconds=0.2
    )

    async with (
        to_server_send,
        to_server_receive,
        to_client_send,
        to_client_receive,
        ClientSession(to_client_receive, to_server_send) as session,
    ):
        result = await provider._call_tool_with_reconnect(
            "slow_tool", {}, "server", session
        )

        requests = []
        while True:
            try:
                requests.append(to_server_receive.receive_nowait())
            except anyio.WouldBlock:
                break

    assert "timed out after 0.2 seconds" in result
    assert "may have completed" in result
    assert len(requests) == requests_sent


@pytest.mark.asyncio
async def test_other_mcp_errors_still_become_an_error_result() -> None:
    async def call_tool(**_: object) -> NoReturn:
        raise McpError(ErrorData(code=INVALID_PARAMS, message="bad arguments"))

    provider = MCPToolsProvider({"server": {"command": "unused"}})
    session = cast("ClientSession", SimpleNamespace(call_tool=call_tool))

    result = await provider._call_tool_with_reconnect(
        "some_tool", {}, "server", session
    )

    assert result == "Error calling MCP tool 'some_tool': bad arguments"
