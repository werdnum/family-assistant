"""The scrape MCP server's stdout carries JSON-RPC and nothing else."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "contrib" / "scrape_mcp.py"


@pytest.mark.no_db
def test_stdout_holds_only_protocol_messages() -> None:
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    ]
    done = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input="".join(json.dumps(request) + "\n" for request in requests),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert done.stdout.strip(), done.stderr
    replies = [json.loads(line) for line in done.stdout.splitlines()]
    assert [reply["id"] for reply in replies] == [1, 2]
