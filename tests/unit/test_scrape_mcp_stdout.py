"""The scrape MCP server's stdout carries JSON-RPC and nothing else."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "contrib" / "scrape_mcp.py"


@pytest.mark.no_db
def test_stdout_holds_only_protocol_messages() -> None:
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}

    with subprocess.Popen(
        [sys.executable, str(SCRIPT)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    ) as server:
        assert server.stdin is not None
        assert server.stdout is not None
        try:
            # The server stops at end of input, even with a request in hand, so
            # each reply has to arrive before the next step.
            server.stdin.write(json.dumps(initialize) + "\n")
            server.stdin.flush()
            first = json.loads(server.stdout.readline())
            server.stdin.write(json.dumps(initialized) + "\n")
            server.stdin.write(json.dumps(ping) + "\n")
            server.stdin.flush()
            second = json.loads(server.stdout.readline())
            server.stdin.close()
            remaining = server.stdout.read()
        finally:
            server.kill()

    assert [first["id"], second["id"]] == [1, 2]
    assert not remaining
