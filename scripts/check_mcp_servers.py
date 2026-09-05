#!/usr/bin/env python3
"""Verify that configured MCP servers start and expose tools.

An MCP server that fails to launch is close to invisible: `MCPToolsProvider`
logs the failure, marks the server `failed`, and the application carries on
serving without the tools that server was supposed to provide. `/health` stays
green, so a container image that ships a broken server passes every check we
had. That is how the `time` server came to be launched through `uvx`, which
re-resolved `mcp-server-time` from PyPI at every startup and eventually landed
on a release that crashes on import against mcp 2.x.

This check closes that gap by connecting to the named servers through the same
`MCPToolsProvider` the application uses — the same environment stripping, the
same initialization timeout — and failing when one of them does not come back
connected with at least one tool. It is run against the built production image
by `scripts/container-smoke-test.sh`, where it sees the real entry points
installed into `/uv/bin` rather than a developer's virtualenv.

Usage:

    python scripts/check_mcp_servers.py time
    python scripts/check_mcp_servers.py --all --timeout 90
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import TYPE_CHECKING, cast

from family_assistant.config_loader import load_config
from family_assistant.config_models import mcp_servers_for_runtime
from family_assistant.tools import MCPServerConfig, MCPToolsProvider
from family_assistant.tools.mcp import MCP_SERVER_STATUS_CONNECTED, MCPServerStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger("check_mcp_servers")


class CheckError(Exception):
    """Raised when the requested check cannot be carried out at all."""


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "server_ids",
        nargs="*",
        help="MCP server ids to check, as named under mcp_config.mcpServers.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Check every configured server instead of naming them.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Seconds to allow for connection and tool discovery (default: 60).",
    )
    return parser.parse_args(argv)


def select_server_configs(
    configured: dict[str, MCPServerConfig],
    requested_ids: Sequence[str],
) -> dict[str, MCPServerConfig]:
    """Narrow the configured servers to those requested.

    An id that is not configured is an error rather than an empty check: the
    caller asked for a guarantee this function cannot give.
    """
    if not requested_ids:
        return dict(configured)

    unknown = [server_id for server_id in requested_ids if server_id not in configured]
    if unknown:
        raise CheckError(
            f"No such MCP server(s) configured: {', '.join(sorted(unknown))}. "
            f"Configured servers: {', '.join(sorted(configured)) or '(none)'}"
        )
    return {server_id: configured[server_id] for server_id in requested_ids}


def failure_reasons(statuses: dict[str, MCPServerStatus]) -> dict[str, str]:
    """Return a reason per server that did not come up healthy."""
    reasons: dict[str, str] = {}
    for server_id, status in statuses.items():
        if status["status"] != MCP_SERVER_STATUS_CONNECTED:
            reasons[server_id] = f"status is '{status['status']}', expected 'connected'"
        elif status["tool_count"] == 0:
            reasons[server_id] = "connected but exposed no tools"
    return reasons


async def check_servers(
    server_configs: dict[str, MCPServerConfig],
    timeout_seconds: int,
) -> dict[str, MCPServerStatus]:
    """Connect to every given server and return its resulting status."""
    provider = MCPToolsProvider(
        mcp_server_configs=server_configs,
        initialization_timeout_seconds=timeout_seconds,
        # The health check loop would retry failures in the background; this is
        # a one-shot verdict on the first connection attempt.
        health_check_interval_seconds=timeout_seconds * 10,
    )
    try:
        await provider.initialize()
        return provider.get_server_statuses()
    finally:
        await provider.close()


def load_configured_servers() -> dict[str, MCPServerConfig]:
    """Read mcp_config.mcpServers from the shipped and operator configuration."""
    config = load_config()
    return {
        server_id: cast("MCPServerConfig", dumped)
        for server_id, dumped in mcp_servers_for_runtime(config.mcp_config).items()
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )

    if not args.server_ids and not args.all:
        print(
            "ERROR: name at least one MCP server id, or pass --all.",
            file=sys.stderr,
        )
        return 2

    try:
        selected = select_server_configs(load_configured_servers(), args.server_ids)
    except CheckError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"Checking {len(selected)} MCP server(s): {', '.join(sorted(selected))}")
    statuses = asyncio.run(check_servers(selected, args.timeout))
    print(json.dumps(statuses, indent=2, sort_keys=True))

    reasons = failure_reasons(statuses)
    if reasons:
        for server_id, reason in sorted(reasons.items()):
            print(f"ERROR: MCP server '{server_id}' {reason}", file=sys.stderr)
        return 1

    print(f"All {len(statuses)} MCP server(s) connected and exposing tools.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
