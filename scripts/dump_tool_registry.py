#!/usr/bin/env python3
"""Write this deployment's tool registry to a snapshot the eval can load.

The eval package resolves every tool name against a registry that defaults to
the *local* tool list compiled into the source tree. A deployment's real
registry also holds whatever its configured MCP servers advertise, discovered at
startup and existing only in a running process, so history extraction and case
replay both drop any tool that came from an MCP server: the name does not
resolve, and the template or case falls out of the harness.

This connects to the configured servers through the same ``MCPToolsProvider``
the application uses, merges what they advertise with the local descriptors, and
writes the result as one JSON file. ``scripts/extract_review_history.py`` and
``scripts/tool_call_review_eval.py`` both read it with ``--tool-registry``, so a
template extracted under a deployment's registry can be replayed under the same
one -- on a machine with no MCP servers, which is where the eval usually runs.

A server that fails to connect aborts the dump. Its tools would simply be absent
from the snapshot, and an absent tool is indistinguishable from one that never
existed: the next extraction would reject those calls and report a smaller
corpus rather than a broken input. Fix the server, or drop it from the
configuration this runs against.

The snapshot is deployment data. MCP parameter schemas can enumerate a
household's own vocabulary -- entity ids, room names, calendar names -- so the
destination resolves through the same private-tree containment rule as the
templates, and ``--allow-external-out`` is the deliberate escape for writing one
somewhere else that is private.

Usage:

    python scripts/dump_tool_registry.py \
        --out .review-eval-local/registry/deployment.json

    # Inside the deployment container, where the MCP servers are configured:
    kubectl exec -n ml-bot deployment/family-assistant -- sh -c \
        '/app/.venv/bin/python /app/scripts/dump_tool_registry.py \
             --out /tmp/registry.json --allow-external-out'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

from family_assistant.config_loader import DEFAULT_CONFIG_FILE, load_config
from family_assistant.eval.private_paths import (
    PrivateEvalPathError,
    resolve_private_eval_path,
)
from family_assistant.eval.tool_call_review.registry_snapshot import (
    descriptors_to_snapshot,
)
from family_assistant.tools import (
    LOCAL_TOOL_DESCRIPTORS,
    MCPServerConfig,
    MCPToolsProvider,
)
from family_assistant.tools.mcp import MCP_SERVER_STATUS_CONNECTED, MCPServerStatus

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from family_assistant.tools.metadata import ToolDescriptor

logger = logging.getLogger("dump_tool_registry")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        required=True,
        help=(
            "Destination JSON file. Resolves inside the gitignored "
            ".review-eval-local/ tree unless --allow-external-out is given."
        ),
    )
    parser.add_argument(
        "--allow-external-out",
        action="store_true",
        help=(
            "Write outside the private eval tree. For a deployment container "
            "that has no repository checkout to resolve the tree against."
        ),
    )
    parser.add_argument(
        "--config-file",
        default=None,
        help=(
            "Operator config overlay to read servers from. Defaults to "
            "$CONFIG_FILE, then config.yaml -- the same resolution the "
            "application entry point uses, so the snapshot describes the "
            "deployment this runs inside."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Seconds to allow each MCP server to initialize (default: 60).",
    )
    parser.add_argument(
        "--local-only",
        action="store_true",
        help=(
            "Snapshot the local tool list without contacting MCP servers. "
            "Produces a registry no larger than the built-in default."
        ),
    )
    return parser.parse_args(argv)


def _resolve_out_path(raw_out: str, *, allow_external: bool) -> Path:
    """Resolve the destination through the private-tree rule unless waived."""
    if allow_external:
        return Path(raw_out).expanduser()
    try:
        return resolve_private_eval_path(raw_out)
    except PrivateEvalPathError as exc:
        raise SystemExit(
            f"Refusing to write the registry snapshot: --out {exc}. MCP "
            "parameter schemas can enumerate a household's own vocabulary; pass "
            "--allow-external-out if this destination is private."
        ) from exc


def _configured_servers(config_file: str | None) -> dict[str, MCPServerConfig]:
    """Read mcp_config.mcpServers from the shipped and operator configuration.

    The overlay is resolved exactly as ``family_assistant.__main__`` resolves it,
    because a snapshot taken from a different configuration than the deployment
    runs describes a registry that deployment does not have -- connecting to
    servers it disabled, or omitting the ones it added. Which is the failure
    this script exists to prevent, arriving through the config file instead of
    through a dead server.
    """
    if config_file is not None:
        # `load_config` treats every overlay as optional and silently omits one
        # that does not exist, so a mistyped path would produce a plausible
        # snapshot of the shipped defaults instead of an error -- this script's
        # own failure again, arriving through a typo. A path named explicitly is
        # required to exist; the env/default path keeps the optional behaviour,
        # because a deployment with no overlay at all is ordinary.
        if not Path(config_file).exists():
            raise SystemExit(
                f"Refusing to read the registry: --config-file {config_file} "
                "does not exist. An overlay that is not there would be skipped "
                "silently and the snapshot would describe the shipped defaults."
            )
        resolved = config_file
    else:
        resolved = os.getenv("CONFIG_FILE", DEFAULT_CONFIG_FILE)
    config = load_config(config_file_path=resolved)
    return {
        server_id: cast("MCPServerConfig", server_config.model_dump())
        for server_id, server_config in config.mcp_config.mcpServers.items()
    }


async def _mcp_descriptors(
    server_configs: dict[str, MCPServerConfig], timeout_seconds: int
) -> list[ToolDescriptor]:
    """Connect to every configured server and return what they advertise."""
    provider = MCPToolsProvider(
        mcp_server_configs=server_configs,
        initialization_timeout_seconds=timeout_seconds,
        # One connection attempt, not a service: a background health loop would
        # reconnect underneath the dump and change what it is writing.
        health_check_interval_seconds=timeout_seconds * 10,
    )
    try:
        await provider.initialize()
        descriptors = await provider.get_tool_descriptors()
        _require_every_server_contributed(
            server_configs, provider.get_server_statuses(), descriptors
        )
        return descriptors
    finally:
        await provider.close()


def _require_every_server_contributed(
    server_configs: Mapping[str, MCPServerConfig],
    statuses: Mapping[str, MCPServerStatus],
    descriptors: Sequence[ToolDescriptor],
) -> None:
    """Abort unless every configured server is connected *and* contributed.

    Connected is not the same as useful: the provider marks a session connected
    before it lists tools, and a server that comes back with an empty list stays
    connected and simply advertises nothing. Both outcomes reach a snapshot the
    same way -- that server's whole surface missing -- and a missing tool is
    indistinguishable from one that never existed, so the next extraction would
    reject those calls and report a smaller corpus instead of a broken input.
    That is the failure this script exists to prevent, so it is not something to
    write down and discover later. `scripts/check_mcp_servers.py` holds the same
    line for the same reason.
    """
    contributed = {
        descriptor.mcp_server_id
        for descriptor in descriptors
        if descriptor.mcp_server_id is not None
    }
    problems = [
        f"{server_id} ({status})"
        for server_id in sorted(server_configs)
        for status in [statuses.get(server_id, {}).get("status", "unknown")]
        if status != MCP_SERVER_STATUS_CONNECTED or server_id not in contributed
    ]
    if problems:
        raise SystemExit(
            "Refusing to write a partial registry snapshot: "
            f"{', '.join(problems)} did not come back connected with at least "
            "one tool. Those tools would be absent from the snapshot, and an "
            "absent tool is indistinguishable from one that never existed -- "
            "the next extraction would reject those calls and report a smaller "
            "corpus. Fix the server, or remove it from the configuration this "
            "runs against."
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    out_path = _resolve_out_path(args.out, allow_external=args.allow_external_out)

    descriptors: list[ToolDescriptor] = list(LOCAL_TOOL_DESCRIPTORS)
    local_count = len(descriptors)
    if args.local_only:
        print(f"Local-only snapshot: {local_count} tool(s), no MCP servers contacted.")
    else:
        server_configs = _configured_servers(args.config_file)
        print(
            f"Connecting to {len(server_configs)} configured MCP server(s): "
            f"{', '.join(sorted(server_configs)) or '(none)'}"
        )
        descriptors.extend(asyncio.run(_mcp_descriptors(server_configs, args.timeout)))
        print(
            f"Discovered {len(descriptors) - local_count} MCP tool(s) "
            f"alongside {local_count} local tool(s)."
        )

    snapshot = descriptors_to_snapshot(descriptors)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    print(f"Wrote {len(descriptors)} tool descriptor(s) to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
