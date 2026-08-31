from __future__ import annotations

import asyncio
import contextlib
import logging
import os  # Import os for environment variable resolution
import random
import time
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    TypedDict,
)  # Added Tuple

import anyio

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mcp import ClientSession
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.sse import sse_client  # Assuming sse_client is in mcp.client.sse
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import TextContent  # Import TextContent from mcp.types

from family_assistant.tools.metadata import (
    ToolDescriptor,
    build_tool_descriptor,
    derive_mcp_annotation_tags,
    normalize_mcp_tool_metadata,
    resolve_mcp_tool_tags,
)

# Import storage functions needed by local tools
# Import the context from the new types file
from .types import (
    MCPServerConfig,
    ToolDefinition,
    ToolExecutionContext,
    ToolNotFoundError,
)

logger = logging.getLogger(__name__)

# Transport type aliases for the Streamable HTTP transport. The MCP spec calls
# this "Streamable HTTP"; we accept several spellings so operators can use
# whichever they're familiar with (Claude Code uses "http").
MCP_STREAMABLE_HTTP_TRANSPORTS = frozenset({
    "streamable_http",
    "streamablehttp",
    "http",
})

# MCP Server Status Constants
MCP_SERVER_STATUS_PENDING = "pending"
MCP_SERVER_STATUS_CONNECTING = "connecting"
MCP_SERVER_STATUS_CONNECTED = "connected"
MCP_SERVER_STATUS_FAILED = "failed"
MCP_SERVER_STATUS_CANCELLED = "cancelled"

# Reconnect pacing defaults. The first retry after a server drops is immediate
# (the next health check cycle); each further attempt without the server being
# seen healthy doubles the wait, up to half an hour.
DEFAULT_RECONNECT_BACKOFF_BASE_SECONDS = 30.0
DEFAULT_RECONNECT_BACKOFF_MAX_SECONDS = 30 * 60.0

# 2**63 seconds already dwarfs any sane cap; clamping the exponent keeps a
# long-lived process from overflowing the float multiplication below.
_MAX_BACKOFF_EXPONENT = 63


def reconnect_backoff_delay(
    attempts: int,
    *,
    base_seconds: float,
    max_seconds: float,
) -> float:
    """Truncated exponential backoff with equal jitter.

    ``attempts`` counts reconnect attempts made since the server was last seen
    healthy. Half of each interval is fixed and half is random, so the wait
    always grows with the attempt count while staying decorrelated from the
    other servers' schedules (and from other instances talking to the same
    remote endpoint).
    """
    if attempts < 1:
        return 0.0
    exponent = min(attempts - 1, _MAX_BACKOFF_EXPONENT)
    ceiling = min(max_seconds, base_seconds * float(2**exponent))
    return random.uniform(ceiling / 2, ceiling)


# MCP transports report a lost connection as a grab bag of exception types, so
# the message text is most of what we have to go on.
_CONNECTION_ERROR_PHRASES = (
    "connection",
    "closed",
    "reset",
    "broken pipe",
    "eof",
    "disconnected",
    "not connected",
)


def _is_connection_error(error: Exception, *, include_timeouts: bool = False) -> bool:
    """Whether ``error`` looks like the transport went away rather than the server misbehaving.

    Timeouts are opt-in: a health check that times out may just be talking to a
    slow server, while a tool call that times out has already burned the
    caller's patience and is worth a reconnect.
    """
    if isinstance(error, anyio.ClosedResourceError):
        return True
    error_str = str(error).lower()
    phrases = (
        (*_CONNECTION_ERROR_PHRASES, "timeout")
        if include_timeouts
        else _CONNECTION_ERROR_PHRASES
    )
    return any(phrase in error_str for phrase in phrases)


@dataclass
class _ReconnectBackoff:
    """Per-server retry pacing for the health check loop.

    ``attempts`` is reset only by a passing health check, not by a successful
    reconnect: a server that accepts a connection and then drops it again is
    just as much in need of pacing as one that refuses outright.
    """

    attempts: int = 0
    next_attempt_at: float = 0.0


class MCPServerStatus(TypedDict):
    """Diagnostic snapshot describing one MCP server's connection state.

    Used by ``MCPToolsProvider.get_server_statuses`` and surfaced through
    the engineer-profile ``get_mcp_server_status`` tool. Token-bearing
    config fields are intentionally omitted.
    """

    status: str
    transport: str
    command: str | None
    args: list[str]
    url: str | None
    session_active: bool
    tool_count: int
    tools: list[str]
    reconnect_attempts: int
    next_reconnect_in_seconds: float | None


class MCPToolsProvider:
    """
    Provides and executes tools hosted on MCP servers.
    Handles connection, fetching definitions, and execution.
    """

    _mcp_server_configs: dict[str, MCPServerConfig]

    def __init__(
        self,
        mcp_server_configs: Mapping[str, MCPServerConfig],
        initialization_timeout_seconds: int = 60,  # Default 1 minute
        health_check_interval_seconds: int = 30,  # Default 30 seconds
        reconnect_backoff_base_seconds: float = DEFAULT_RECONNECT_BACKOFF_BASE_SECONDS,
        reconnect_backoff_max_seconds: float = DEFAULT_RECONNECT_BACKOFF_MAX_SECONDS,
    ) -> None:
        self._mcp_server_configs = dict(mcp_server_configs)
        self._initialization_timeout_seconds = initialization_timeout_seconds
        self._health_check_interval_seconds = health_check_interval_seconds
        self._reconnect_backoff_base_seconds = reconnect_backoff_base_seconds
        self._reconnect_backoff_max_seconds = reconnect_backoff_max_seconds
        self._reconnect_backoff: dict[str, _ReconnectBackoff] = {
            server_id: _ReconnectBackoff() for server_id in self._mcp_server_configs
        }
        self._sessions: dict[str, ClientSession] = {}
        self._tool_map: dict[str, str] = {}  # Map tool name -> server_id
        self._definitions: list[ToolDefinition] = []
        self._descriptors: list[ToolDescriptor] = []
        self._descriptors_version = 0
        self._initialized = False
        self._connection_contexts: dict[str, contextlib.AsyncExitStack] = {}
        self._server_statuses: dict[str, str] = {
            server_id: MCP_SERVER_STATUS_PENDING
            for server_id in self._mcp_server_configs
        }
        self._health_check_task: asyncio.Task | None = None
        self._health_check_enabled = True
        logger.info(
            f"MCPToolsProvider created for {len(self._mcp_server_configs)} configured servers. "
            f"Initialization timeout: {self._initialization_timeout_seconds}s. "
            f"Health check interval: {self._health_check_interval_seconds}s. "
            f"Reconnect backoff: {self._reconnect_backoff_base_seconds}s base, "
            f"{self._reconnect_backoff_max_seconds}s max. "
            f"Initialization pending."
        )

    @property
    def server_configs(self) -> dict[str, MCPServerConfig]:
        """Returns the configured MCP servers."""
        return self._mcp_server_configs

    def get_server_statuses(self) -> dict[str, MCPServerStatus]:
        """Return a snapshot of MCP server connection status for diagnostics.

        Returns a mapping of ``server_id`` to an ``MCPServerStatus`` describing
        the current connection state, transport, configured connection
        details (no tokens), session activity, the tools currently provided by
        that server, and where the server sits in the reconnect backoff
        schedule.

        Designed to be called by engineer-profile diagnostic tools without
        requiring any further reconnection or I/O.
        """
        # Invert the tool_map so we can list tools per-server without
        # touching internal storage in callers.
        tools_by_server: dict[str, list[str]] = {
            server_id: [] for server_id in self._mcp_server_configs
        }
        for tool_name, server_id in self._tool_map.items():
            tools_by_server.setdefault(server_id, []).append(tool_name)

        snapshot: dict[str, MCPServerStatus] = {}
        now = time.monotonic()
        for server_id, config in self._mcp_server_configs.items():
            tools = sorted(tools_by_server.get(server_id, []))
            backoff = self._reconnect_backoff[server_id]
            snapshot[server_id] = MCPServerStatus(
                status=self._server_statuses.get(server_id, MCP_SERVER_STATUS_PENDING),
                transport=config.get("transport", "stdio"),
                command=config.get("command"),
                args=list(config.get("args", []) or []),
                url=config.get("url"),
                session_active=server_id in self._sessions,
                tool_count=len(tools),
                tools=tools,
                reconnect_attempts=backoff.attempts,
                next_reconnect_in_seconds=(
                    round(max(0.0, backoff.next_attempt_at - now), 1)
                    if backoff.attempts
                    else None
                ),
            )
        return snapshot

    def _build_mcp_descriptors(
        self,
        server_id: str,
        definitions: Sequence[ToolDefinition],
        discovered_tools: Sequence[Any],
    ) -> list[ToolDescriptor]:
        """Build descriptors for discovered MCP tools."""
        configured_tool_metadata = normalize_mcp_tool_metadata(
            self._mcp_server_configs[server_id].get("tool_metadata")
        )
        descriptors: list[ToolDescriptor] = []
        discovered_tools_by_name = {
            discovered_tool.name: discovered_tool
            for discovered_tool in discovered_tools
            if getattr(discovered_tool, "name", None)
        }

        for definition in definitions:
            tool_name = definition["function"]["name"]
            discovered_tool = discovered_tools_by_name.get(tool_name)
            annotations = getattr(discovered_tool, "annotations", None)
            annotation_tags = derive_mcp_annotation_tags(
                read_only_hint=getattr(annotations, "readOnlyHint", None),
                destructive_hint=getattr(annotations, "destructiveHint", None),
                open_world_hint=getattr(annotations, "openWorldHint", None),
            )
            tags = resolve_mcp_tool_tags(
                tool_name=tool_name,
                configured_tool_metadata=configured_tool_metadata,
                annotation_tags=annotation_tags,
            )
            descriptors.append(
                build_tool_descriptor(
                    definition,
                    tags,
                    origin="mcp",
                    mcp_server_id=server_id,
                )
            )

        return descriptors

    async def _list_all_tools(self, session: ClientSession) -> list[Any]:
        """Read a server's complete tool list, following pagination cursors.

        ``tools/list`` is paginated, so the first page alone is not the
        server's answer: reconciling against it would treat everything past
        that page as withdrawn.
        """
        tools: list[Any] = []
        cursor: str | None = None
        while True:
            response = await session.list_tools(cursor=cursor)
            tools.extend(response.tools)
            cursor = response.nextCursor
            if cursor is None:
                return tools

    async def _log_mcp_initialization_progress(
        self, stop_event: asyncio.Event, start_time: float
    ) -> None:
        """Logs progress during MCP tool initialization."""
        logger.debug("MCP initialization logging task started.")

        async def log_until_stopped() -> None:
            while not stop_event.is_set():
                try:
                    # Wait for 10 seconds or until stop_event is set
                    await asyncio.wait_for(stop_event.wait(), timeout=10.0)
                except TimeoutError:
                    # Timeout occurred, meaning 10 seconds passed and stop_event is not set
                    if not stop_event.is_set():  # Double check
                        current_time = asyncio.get_running_loop().time()
                        elapsed_time = current_time - start_time
                        remaining_time = (
                            self._initialization_timeout_seconds - elapsed_time
                        )
                        pending_servers = [
                            sid
                            for sid, status in self._server_statuses.items()
                            if status
                            in {
                                MCP_SERVER_STATUS_PENDING,
                                MCP_SERVER_STATUS_CONNECTING,
                            }
                        ]
                        logger.info(
                            f"Still initializing MCP tools... "
                            f"Waiting for {len(pending_servers)} of {len(self._mcp_server_configs)} server(s): {', '.join(pending_servers) if pending_servers else 'None'}. "
                            f"Elapsed: {elapsed_time:.0f}s. "
                            f"Timeout in approx {max(0, remaining_time):.0f}s (total {self._initialization_timeout_seconds}s)."
                        )
                except asyncio.CancelledError:  # If logging_task itself is cancelled
                    raise

        try:
            await log_until_stopped()
        except asyncio.CancelledError:
            logger.debug("MCP initialization logging task cancelled.")
        except Exception as e:
            logger.exception(
                f"Unexpected error in MCP initialization logging task: {e}"
            )
        finally:
            logger.debug("MCP initialization logging task finished.")

    async def _connect_and_discover_mcp(
        self,
        server_id: str,
        server_conf: MCPServerConfig,
    ) -> tuple[
        ClientSession | None,
        list[ToolDefinition],
        list[ToolDescriptor],
        dict[str, str],
    ]:
        """Connects to a single MCP server, discovers tools, and returns results."""
        self._server_statuses[server_id] = MCP_SERVER_STATUS_CONNECTING
        discovered_tools = []
        tool_map = {}
        exit_stack = contextlib.AsyncExitStack()

        transport_type = server_conf.get("transport", "stdio").lower()
        url = server_conf.get("url")  # Needed for SSE
        token_config = server_conf.get(
            "token"
        )  # New dedicated token field for SSE/HTTP
        command = server_conf.get("command")  # Needed for STDIO
        args = server_conf.get("args", [])  # Needed for STDIO
        env_config = server_conf.get("env")  # Env config primarily for STDIO now

        # --- Resolve environment variable placeholders for STDIO ---
        resolved_env_stdio = None  # Renamed for clarity
        if isinstance(env_config, dict):
            resolved_env_stdio = {}
            for key, value in env_config.items():
                if isinstance(value, str) and value.startswith("$"):
                    env_var_name = value[1:]  # Remove the leading '$'
                    resolved_value = os.getenv(env_var_name)
                    if resolved_value is not None:
                        resolved_env_stdio[key] = (
                            resolved_value  # Fix typo: use resolved_env_stdio
                        )
                        logger.debug(
                            f"Resolved env var '{env_var_name}' for MCP server '{server_id}'"
                        )
                    else:
                        logger.warning(
                            f"Env var '{env_var_name}' for MCP server '{server_id}' not found in environment. Omitting."
                        )
                else:
                    resolved_env_stdio[key] = value  # Fix typo: use resolved_env_stdio
        elif env_config is not None:
            logger.warning(
                f"MCP server '{server_id}' has non-dictionary 'env' configuration for stdio. Ignoring."
            )
        # --- End environment variable resolution for STDIO ---

        # --- Resolve token from config or environment variable for SSE/HTTP ---
        resolved_token_sse = None
        if token_config and isinstance(token_config, str):
            if token_config.startswith("$"):
                token_env_var_name = token_config[1:]
                resolved_token_sse = os.getenv(token_env_var_name)
                if resolved_token_sse:
                    logger.debug(
                        f"Resolved token env var '{token_env_var_name}' for MCP server '{server_id}'"
                    )
                else:
                    logger.warning(
                        f"Token env var '{token_env_var_name}' for MCP server '{server_id}' not found in environment."
                    )
            else:
                # Assume the token value is provided directly in the config
                resolved_token_sse = token_config
        elif token_config:
            logger.warning(
                f"MCP server '{server_id}' has non-string 'token' configuration. Ignoring."
            )
        # --- End token resolution ---

        logger.info(
            f"Attempting connection and discovery for MCP server '{server_id}' using '{transport_type}' transport..."
        )

        async def connect_and_discover() -> tuple[
            ClientSession | None,
            list[ToolDefinition],
            list[ToolDescriptor],
            dict[str, str],
        ]:
            # --- Transport and Session Creation ---
            if transport_type == "stdio":
                if not command:
                    logger.error(
                        f"MCP server '{server_id}' (stdio): 'command' is missing."
                    )
                    self._server_statuses[server_id] = MCP_SERVER_STATUS_FAILED
                    with contextlib.suppress(Exception):
                        await exit_stack.aclose()
                    return None, [], [], {}
                server_params = StdioServerParameters(
                    command=command,
                    args=args,
                    env=resolved_env_stdio,  # Use stdio-specific env vars
                )

                read_stream, write_stream = await exit_stack.enter_async_context(
                    stdio_client(server_params)
                )

                session = await exit_stack.enter_async_context(
                    ClientSession(read_stream, write_stream)
                )

                self._connection_contexts[server_id] = exit_stack

            elif (
                transport_type == "sse"
                or transport_type in MCP_STREAMABLE_HTTP_TRANSPORTS
            ):
                is_streamable_http = transport_type in MCP_STREAMABLE_HTTP_TRANSPORTS
                transport_label = "streamable_http" if is_streamable_http else "sse"
                if not url:
                    logger.error(
                        f"MCP server '{server_id}' ({transport_label}): 'url' is missing."
                    )
                    self._server_statuses[server_id] = MCP_SERVER_STATUS_FAILED
                    with contextlib.suppress(Exception):
                        await exit_stack.aclose()
                    return None, [], [], {}

                # Construct headers using the resolved token
                headers = {}
                if resolved_token_sse:
                    headers["Authorization"] = f"Bearer {resolved_token_sse}"
                    logger.debug(
                        f"Using Authorization header for {transport_label} server '{server_id}'."
                    )
                else:
                    logger.warning(
                        f"No token resolved for {transport_label} server '{server_id}'. "
                        "Connecting without Authorization header."
                    )
                    # Add other potential header mappings here if needed

                if is_streamable_http:
                    # streamablehttp_client yields a 3-tuple; the third element is a
                    # callback for retrieving the negotiated session id, which we
                    # don't need here.
                    (
                        read_stream,
                        write_stream,
                        _get_session_id,
                    ) = await exit_stack.enter_async_context(
                        streamablehttp_client(url=url, headers=headers)
                    )
                else:
                    read_stream, write_stream = await exit_stack.enter_async_context(
                        sse_client(url=url, headers=headers)
                    )

                session = await exit_stack.enter_async_context(
                    ClientSession(read_stream, write_stream)
                )

                self._connection_contexts[server_id] = exit_stack

            else:
                logger.error(
                    f"Unsupported transport type '{transport_type}' for MCP server '{server_id}'."
                )
                self._server_statuses[server_id] = MCP_SERVER_STATUS_FAILED
                with contextlib.suppress(Exception):
                    await exit_stack.aclose()
                return None, [], [], {}

            # --- Initialize Session and Discover Tools (Common Logic) ---
            await session.initialize()
            self._server_statuses[server_id] = MCP_SERVER_STATUS_CONNECTED
            logger.info(
                f"Initialized session with MCP server '{server_id}' ({transport_type}). Status: {self._server_statuses[server_id]}."
            )

            server_tools = await self._list_all_tools(session)
            logger.info(f"Server '{server_id}' provides {len(server_tools)} tools.")

            # Format MCP tools to OpenAI dict format (sanitization moved to LLM layer)
            sanitized_tools = self._format_mcp_definitions_to_dicts(server_tools)
            discovered_tools.extend(sanitized_tools)

            for tool_def in sanitized_tools:  # Iterate sanitized definitions
                func_def = tool_def.get("function", {})
                tool_name = func_def.get("name")
                if tool_name:
                    tool_map[tool_name] = (
                        server_id  # Map name to server_id for this task's result
                    )
                else:
                    logger.warning(
                        f"Found tool definition without a name on server '{server_id}': {tool_def}"
                    )

            discovered_descriptors = self._build_mcp_descriptors(
                server_id=server_id,
                definitions=sanitized_tools,
                discovered_tools=server_tools,
            )

            return session, discovered_tools, discovered_descriptors, tool_map

        try:
            return await connect_and_discover()
        except Exception as e:
            logger.exception(
                f"Failed connection/discovery for MCP server '{server_id}': {e}"
            )
            self._server_statuses[server_id] = MCP_SERVER_STATUS_FAILED
            with contextlib.suppress(Exception):
                await exit_stack.aclose()
            # Clean up any partially created contexts
            if server_id in self._connection_contexts:
                await self._close_server_connections(server_id)
            return None, [], [], {}  # Return empty on failure

    async def initialize(self) -> None:
        """Connects to configured MCP servers, fetches and sanitizes tool definitions."""
        if self._initialized:
            return

        logger.info(
            f"Initializing MCPToolsProvider: Connecting to {len(self._mcp_server_configs)} servers..."
        )

        self._sessions = {}
        self._tool_map = {}
        self._definitions = []
        self._descriptors = []
        # Reset server statuses to PENDING if re-initializing
        self._server_statuses = {
            sid: MCP_SERVER_STATUS_PENDING for sid in self._mcp_server_configs
        }

        # --- Create connection tasks ---
        connection_tasks = [
            self._connect_and_discover_mcp(server_id, server_conf)
            for server_id, server_conf in self._mcp_server_configs.items()
        ]

        # --- Run tasks concurrently with logging and timeout ---
        stop_logging_event = asyncio.Event()
        initialization_start_time = asyncio.get_running_loop().time()
        logging_task = asyncio.create_task(
            self._log_mcp_initialization_progress(
                stop_logging_event, initialization_start_time
            )
        )

        connection_tasks_future = asyncio.gather(
            *connection_tasks, return_exceptions=True
        )
        results: list[Any] = []  # Default to empty list

        try:
            logger.info(
                f"Waiting for MCP server connections with a timeout of {self._initialization_timeout_seconds} seconds..."
            )
            results = await asyncio.wait_for(
                connection_tasks_future, timeout=self._initialization_timeout_seconds
            )
            logger.info("Finished parallel MCP connection attempts (within timeout).")
        except TimeoutError:
            logger.error(
                f"MCPToolsProvider initialization timed out after {self._initialization_timeout_seconds} seconds. "
                "Proceeding with any tools discovered before timeout or if tasks completed with errors."
            )
            # If gather was cancelled by timeout, its tasks might have CancelledError.
            # We try to get results if the future is done.
            if connection_tasks_future.done():
                try:
                    # This might raise CancelledError if gather itself was cancelled before completing
                    # its internal result collection.
                    results = connection_tasks_future.result()
                except asyncio.CancelledError:
                    logger.warning(
                        "MCP connection gather operation was cancelled by timeout before all results could be collected."
                    )
                    # results remains as its last assigned value (potentially empty or partial from a previous attempt if any)
                    # or its initial empty list. This is handled by the processing loop below.
            # If not done, results remains empty, which is also handled.
        except Exception as e:
            logger.exception(f"Unexpected error during MCP connection gathering: {e}")
            # Try to get results if possible
            if (
                connection_tasks_future.done()
                and not connection_tasks_future.cancelled()
            ):
                results = connection_tasks_future.result()
        finally:
            stop_logging_event.set()
            if not logging_task.done():
                try:
                    await asyncio.wait_for(logging_task, timeout=1.0)
                except TimeoutError:
                    logging_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await logging_task
                except (
                    asyncio.CancelledError
                ):  # If logging_task itself was cancelled externally
                    with contextlib.suppress(asyncio.CancelledError):
                        await logging_task
            # If logging_task was already done (e.g. error), no need to await/cancel.

        # --- Process results ---
        # This loop will process whatever results were gathered, even if it's an empty list
        # or a list containing exceptions (including CancelledError for timed-out tasks).
        for i, res_item in enumerate(results):
            # Ensure server_id is safely accessed if results list is shorter than expected
            if i < len(self._mcp_server_configs):
                server_id = list(self._mcp_server_configs.keys())[i]
            else:
                logger.warning(
                    f"Result item at index {i} has no corresponding server_id due to partial results. Item: {res_item}"
                )
                continue  # Skip processing this anomalous result item

            if isinstance(res_item, BaseException):
                # This includes asyncio.CancelledError if a task was cancelled by the timeout
                if isinstance(res_item, asyncio.CancelledError):
                    logger.warning(
                        f"Connection/discovery for MCP server '{server_id}' was cancelled (likely due to timeout)."
                    )
                    self._server_statuses[server_id] = MCP_SERVER_STATUS_CANCELLED
                else:
                    logger.error(
                        f"Gather caught exception for server '{server_id}': {res_item}"
                    )
                    # Status should have been set to FAILED by _connect_and_discover_mcp
                    if self._server_statuses[server_id] not in {
                        MCP_SERVER_STATUS_FAILED,
                        MCP_SERVER_STATUS_CANCELLED,
                    }:
                        self._server_statuses[server_id] = MCP_SERVER_STATUS_FAILED
            elif res_item is None:
                logger.warning(
                    f"Received None result for server '{server_id}' from task (should be tuple)."
                )
                self._server_statuses[server_id] = MCP_SERVER_STATUS_FAILED
            else:
                (
                    session,
                    discovered_tools,
                    descriptors_for_server,
                    _tool_map_for_server,
                ) = res_item
                if session:
                    # Status should be CONNECTED from _connect_and_discover_mcp
                    self._sessions[server_id] = session
                    self._register_server_tools(
                        server_id, discovered_tools, descriptors_for_server
                    )
                else:
                    logger.warning(
                        f"Connection/discovery for MCP server '{server_id}' completed but yielded no active session. Result: {res_item}"
                    )
                    if self._server_statuses[server_id] == MCP_SERVER_STATUS_CONNECTING:
                        # If it was connecting but no session, mark failed
                        self._server_statuses[server_id] = MCP_SERVER_STATUS_FAILED

        self._initialized = True
        self._bump_descriptors_version()
        # Summarize outcomes based on statuses
        connected_count = sum(
            1
            for status in self._server_statuses.values()
            if status == MCP_SERVER_STATUS_CONNECTED
        )
        failed_count = sum(
            1
            for status in self._server_statuses.values()
            if status == MCP_SERVER_STATUS_FAILED
        )
        cancelled_count = sum(
            1
            for status in self._server_statuses.values()
            if status == MCP_SERVER_STATUS_CANCELLED
        )
        initialization_end_time = asyncio.get_running_loop().time()
        total_initialization_time = initialization_end_time - initialization_start_time
        logger.info(
            f"MCPToolsProvider finished processing all {len(self._mcp_server_configs)} configured MCP server(s) in {total_initialization_time:.2f} seconds. "
            f"Summary: {connected_count} connected, {failed_count} failed, {cancelled_count} cancelled. "
            f"Active sessions: {len(self._sessions)}. Mapped {len(self._tool_map)} unique tools from {len(self._definitions)} total definitions."
        )

        # Start health check task to monitor connected servers and retry failed/cancelled ones
        if self._health_check_enabled:
            self._health_check_task = asyncio.create_task(self._health_check_loop())
            logger.info("Started MCP server health check task")

    def _format_mcp_definitions_to_dicts(
        # self, definitions: List[Dict[str, Any]] # Original signature
        self,
        definitions: Sequence[Any],  # MCP list_tools returns Tool objects
    ) -> list[ToolDefinition]:
        """
        Accepts a list of MCP Tool objects.
        Converts MCP Tool objects to OpenAI-like dictionary format.
        Sanitization (removing unsupported formats) is handled by the LLM client layer.
        """
        formatted_defs = []
        for tool in definitions:  # Iterate MCP Tool objects
            try:
                # Convert MCP Tool object to OpenAI-like dictionary format
                tool_dict = {
                    "type": "function",
                    "function": {
                        "name": (
                            tool.name
                        ),  # Assuming these attributes exist on MCP Tool object
                        "description": (
                            tool.description
                        ),  # Assuming these attributes exist
                        "parameters": tool.inputSchema,
                    },
                }
                # --- Sanitization logic removed from here ---
                # The 'format' field might still be present in the 'parameters' dict

                formatted_defs.append(tool_dict)  # Add the formatted dict
            except Exception as e:
                logger.exception(
                    f"Error formatting MCP tool definition to dict: {getattr(tool, 'name', 'UnknownName')}. Error: {e}"
                )

        return formatted_defs

    async def get_tool_definitions(
        self,
    ) -> list[ToolDefinition]:
        """Returns the aggregated and sanitized tool definitions from all connected servers."""
        if not self._initialized:
            await self.initialize()
        return self._definitions

    @property
    def descriptors_version(self) -> int:
        """Monotonic counter that changes whenever the descriptor set changes.

        Downstream wrappers cache policy-filtered tool listings for the process
        lifetime. They compare this counter against the version their cache was
        built at, so a server connecting, reconnecting, or disconnecting forces
        a rebuild instead of serving a stale list. Without this, an MCP server
        that is down at startup stays unadvertisable even after the health-check
        loop reconnects it.
        """
        return self._descriptors_version

    def _bump_descriptors_version(self) -> None:
        """Signal that the discovered descriptor set has changed."""
        self._descriptors_version += 1

    async def get_tool_descriptors(self) -> list[ToolDescriptor]:
        """Return descriptors for discovered MCP tools."""
        if not self._initialized:
            await self.initialize()
        return list(self._descriptors)

    async def get_tool_descriptor(self, name: str) -> ToolDescriptor | None:
        """Return a single MCP tool descriptor by name."""
        if not self._initialized:
            await self.initialize()
        for descriptor in self._descriptors:
            if descriptor.name == name:
                return descriptor
        return None

    async def _health_check_loop(self) -> None:
        """Periodically checks the health of connected MCP servers and retries failed/cancelled ones."""
        logger.info(
            f"Starting health check loop with interval {self._health_check_interval_seconds}s"
        )

        async def run_health_check_iteration() -> bool:
            # Wait for the interval
            await asyncio.sleep(self._health_check_interval_seconds)

            if not self._health_check_enabled:
                return False

            # Collect servers needing retry before health checks run,
            # so servers that fail health check below aren't retried twice
            servers_to_retry = [
                server_id
                for server_id, status in self._server_statuses.items()
                if status in {MCP_SERVER_STATUS_FAILED, MCP_SERVER_STATUS_CANCELLED}
            ]

            await self._run_health_checks()
            await self._retry_disconnected_servers(servers_to_retry)
            return True

        while self._health_check_enabled:
            try:
                should_continue = await run_health_check_iteration()
            except asyncio.CancelledError:
                logger.info("Health check loop cancelled")
                break
            except Exception as e:
                logger.exception(f"Unexpected error in health check loop: {e}")
                # Continue the loop despite errors
                continue

            if not should_continue:
                break

        logger.info("Health check loop stopped")

    async def _run_health_checks(self) -> None:
        """Ping every live session, reconnecting the ones that have died.

        A passing check is the only thing that clears a server's reconnect
        backoff: a successful reconnect proves the endpoint accepted one
        connection, whereas surviving a full interval proves it is usable.
        """
        for server_id, session in list(self._sessions.items()):
            if not self._health_check_enabled:
                return

            try:
                # Simple health check - list tools to verify connection
                # Using a short timeout to avoid blocking too long
                server_tools = await asyncio.wait_for(
                    self._list_all_tools(session), timeout=5.0
                )
            except TimeoutError:
                logger.warning(f"Health check timeout for server '{server_id}'")
                # Don't reconnect on timeout - server might just be slow
            except Exception as e:
                logger.warning(f"Health check failed for server '{server_id}': {e}")
                if not _is_connection_error(e):
                    continue

                logger.info(
                    f"Detected connection issue for server '{server_id}', dropping session"
                )
                self._server_statuses[server_id] = MCP_SERVER_STATUS_FAILED
                # Drop the dead session even if the backoff window defers the
                # reconnect, so it isn't pinged again on every later cycle.
                await self._teardown_server(server_id)
                await self._attempt_scheduled_reconnect(
                    server_id, reason="health check"
                )
            else:
                logger.debug(f"Health check passed for server '{server_id}'")
                self._reset_reconnect_backoff(server_id)
                self._refresh_server_tools(server_id, server_tools)

    async def _retry_disconnected_servers(self, server_ids: Sequence[str]) -> None:
        """Reconnect failed/cancelled servers whose backoff window has elapsed."""
        for server_id in server_ids:
            if not self._health_check_enabled:
                return
            await self._attempt_scheduled_reconnect(
                server_id, reason=f"previously {self._server_statuses[server_id]}"
            )

    async def _attempt_scheduled_reconnect(
        self, server_id: str, *, reason: str
    ) -> bool:
        """Reconnect ``server_id`` unless it is still inside its backoff window.

        Every attempt extends the window, whether or not it succeeded; only a
        subsequent passing health check resets it. Returns whether a
        reconnection actually happened, so a deferred attempt and a failed one
        both report ``False``.
        """
        backoff = self._reconnect_backoff[server_id]
        now = time.monotonic()
        if now < backoff.next_attempt_at:
            logger.debug(
                "Deferring reconnect of MCP server '%s' (%s): backing off for another "
                "%.0fs after %d attempt(s)",
                server_id,
                reason,
                backoff.next_attempt_at - now,
                backoff.attempts,
            )
            return False

        logger.info(
            "Reconnect attempt %d for MCP server '%s' (%s)",
            backoff.attempts + 1,
            server_id,
            reason,
        )
        reconnected = await self._reconnect_server(server_id)

        backoff.attempts += 1
        delay = reconnect_backoff_delay(
            backoff.attempts,
            base_seconds=self._reconnect_backoff_base_seconds,
            max_seconds=self._reconnect_backoff_max_seconds,
        )
        backoff.next_attempt_at = time.monotonic() + delay
        if reconnected:
            logger.info(
                "Successfully reconnected MCP server '%s' on attempt %d",
                server_id,
                backoff.attempts,
            )
        else:
            logger.warning(
                "Reconnect attempt %d for MCP server '%s' failed; next attempt in ~%.0fs",
                backoff.attempts,
                server_id,
                delay,
            )
        return reconnected

    def _reset_reconnect_backoff(self, server_id: str) -> None:
        """Forget a server's retry history after it has been seen healthy."""
        backoff = self._reconnect_backoff[server_id]
        if backoff.attempts:
            logger.info(
                "MCP server '%s' is healthy again; clearing reconnect backoff "
                "after %d attempt(s)",
                server_id,
                backoff.attempts,
            )
        backoff.attempts = 0
        backoff.next_attempt_at = 0.0

    async def reconnect_server(self, server_id: str) -> bool:
        """Public wrapper around the internal reconnect routine.

        Used by diagnostic tools (e.g. the engineer profile's
        ``reconnect_mcp_server``) so callers don't have to reach into a
        private method. An operator asking for a reconnect knows something the
        backoff schedule doesn't, so this ignores the current window and, on
        success, clears it.
        """
        if server_id not in self._mcp_server_configs:
            raise KeyError(server_id)
        reconnected = await self._reconnect_server(server_id)
        if reconnected:
            self._reset_reconnect_backoff(server_id)
        return reconnected

    def _registered_descriptors(self, server_id: str) -> list[ToolDescriptor]:
        """Return the descriptors currently registered on behalf of a server."""
        return [
            descriptor
            for descriptor in self._descriptors
            if descriptor.mcp_server_id == server_id
        ]

    def _unregister_server_tools(self, server_id: str) -> None:
        """Forget the tools a server provided, leaving its session untouched."""
        tools_to_remove = {
            name for name, sid in self._tool_map.items() if sid == server_id
        }
        for tool_name in tools_to_remove:
            del self._tool_map[tool_name]

        self._definitions = [
            d
            for d in self._definitions
            if d.get("function", {}).get("name") not in tools_to_remove
        ]
        self._descriptors = [
            descriptor
            for descriptor in self._descriptors
            if descriptor.mcp_server_id != server_id
        ]

    def _register_server_tools(
        self,
        server_id: str,
        definitions: Sequence[ToolDefinition],
        descriptors: Sequence[ToolDescriptor],
    ) -> None:
        """Register a server's discovered tools, skipping names already taken.

        Callers unregister the server's previous tools first, so a name still
        in the map belongs to a different server and keeps its owner.
        """
        for definition, descriptor in zip(definitions, descriptors, strict=False):
            tool_name = descriptor.name
            if tool_name in self._tool_map:
                logger.warning(
                    "Skipping duplicate tool '%s' from server '%s' "
                    "(already provided by '%s')",
                    tool_name,
                    server_id,
                    self._tool_map[tool_name],
                )
                continue
            self._definitions.append(definition)
            self._descriptors.append(descriptor)
            self._tool_map[tool_name] = server_id

    def _refresh_server_tools(
        self, server_id: str, server_tools: Sequence[Any]
    ) -> None:
        """Reconcile a server's cached tools with what it just reported.

        A server that answered the initial ``list_tools`` with the wrong list —
        most damagingly an empty one — would otherwise keep it for the life of
        the process: the connection is healthy, so nothing reconnects it, and
        nothing else re-reads its tools. The health check already asks for that
        list, so it is also what keeps the cache honest.
        """
        definitions = self._format_mcp_definitions_to_dicts(server_tools)
        descriptors = self._build_mcp_descriptors(
            server_id=server_id,
            definitions=definitions,
            discovered_tools=server_tools,
        )
        # Compare what registration would produce, not the raw report, so a
        # name another server owns doesn't look like a change on every cycle.
        # Descriptors rather than definitions, because the annotation-derived
        # tags that drive policy matching live only on the descriptor: a tool
        # that keeps its schema but stops being read-only has changed. Keyed by
        # name rather than ordered, so a server that shuffles its list is not a
        # change either.
        prospective = self._prospective_registration(server_id, descriptors)
        previous = {
            descriptor.name: descriptor
            for descriptor in self._registered_descriptors(server_id)
        }
        if prospective == previous:
            return

        self._unregister_server_tools(server_id)
        self._register_server_tools(server_id, definitions, descriptors)
        self._bump_descriptors_version()
        logger.info(
            "MCP server '%s' reported a changed tool list on health check: "
            "now %d tool(s) (added: %s; removed: %s)",
            server_id,
            len(prospective),
            ", ".join(sorted(prospective.keys() - previous.keys())) or "none",
            ", ".join(sorted(previous.keys() - prospective.keys())) or "none",
        )

    def _prospective_registration(
        self, server_id: str, descriptors: Sequence[ToolDescriptor]
    ) -> dict[str, ToolDescriptor]:
        """The descriptors ``_register_server_tools`` would keep, keyed by name.

        Mirrors registration's two rules — a name another server owns stays
        with that server, and the first of a repeated name wins — so that
        comparing against it never reports a change registration can't make.
        """
        prospective: dict[str, ToolDescriptor] = {}
        for descriptor in descriptors:
            if self._tool_map.get(descriptor.name) not in {None, server_id}:
                continue
            prospective.setdefault(descriptor.name, descriptor)
        return prospective

    async def _teardown_server(self, server_id: str) -> None:
        """Drop a server's session and unregister the tools it provided."""
        # Close existing session and connection if any
        if server_id in self._sessions:
            try:
                # Remove from sessions to prevent reuse during reconnection
                self._sessions.pop(server_id)
                # Close the context managers for this server
                await self._close_server_connections(server_id)
            except Exception as e:
                logger.warning(f"Error removing old session for '{server_id}': {e}")

        self._unregister_server_tools(server_id)
        # The descriptor set shrank; nothing here puts it back.
        self._bump_descriptors_version()

    async def _reconnect_server(self, server_id: str) -> bool:
        """Attempts to reconnect a single MCP server."""
        logger.info(f"Attempting to reconnect MCP server '{server_id}'...")

        # Get the server config
        server_conf = self._mcp_server_configs.get(server_id)
        if not server_conf:
            logger.error(f"No configuration found for server '{server_id}'")
            return False

        await self._teardown_server(server_id)

        # Attempt reconnection
        async def reconnect() -> bool:
            # Call the existing connection method
            (
                session,
                discovered_tools,
                discovered_descriptors,
                tool_map,
            ) = await self._connect_and_discover_mcp(server_id, server_conf)

            if session:
                self._sessions[server_id] = session
                self._register_server_tools(
                    server_id, discovered_tools, discovered_descriptors
                )
                self._bump_descriptors_version()
                logger.info(
                    f"Successfully reconnected MCP server '{server_id}' with {len(discovered_tools)} tools"
                )
                return True
            else:
                logger.error(f"Failed to reconnect MCP server '{server_id}'")
                return False

        try:
            return await reconnect()
        except Exception as e:
            logger.exception(f"Error reconnecting MCP server '{server_id}': {e}")
            self._server_statuses[server_id] = MCP_SERVER_STATUS_FAILED
            return False

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - MCP tool arguments are untyped per the MCP protocol
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str:
        """Executes an MCP tool on the appropriate server with automatic reconnection on failure."""
        if not self._initialized:
            await self.initialize()  # Ensure connections and mapping are ready

        server_id = self._tool_map.get(name)
        if not server_id:
            raise ToolNotFoundError(f"MCP tool '{name}' not found in tool map.")

        session = self._sessions.get(server_id)
        if not session:
            # This might happen if the server failed to connect during initialize
            logger.error(
                f"Session for server '{server_id}' (tool '{name}') not found or inactive."
            )
            raise ToolNotFoundError(f"Session for MCP tool '{name}' is unavailable.")

        logger.info(
            f"Executing MCP tool '{name}' on server '{server_id}' with args: {arguments}"
        )

        # Try to execute the tool, with one reconnection attempt on failure
        for attempt in range(2):

            async def call_tool_result(active_session: ClientSession) -> str:
                mcp_result = await active_session.call_tool(
                    name=name, arguments=arguments
                )

                # Process MCP result content
                response_parts = []
                if mcp_result.content:
                    for content_item in mcp_result.content:
                        if isinstance(content_item, TextContent) and content_item.text:
                            response_parts.append(content_item.text)
                        # Handle other content types if needed (e.g., image, resource)

                result_str = (
                    "\n".join(response_parts)
                    if response_parts
                    else "Tool executed successfully."
                )

                if mcp_result.isError:
                    logger.error(
                        f"MCP tool '{name}' on server '{server_id}' returned an error: {result_str}"
                    )
                    return f"Error executing tool '{name}': {result_str}"  # Prepend error indication
                else:
                    logger.info(
                        f"MCP tool '{name}' on server '{server_id}' executed successfully."
                    )
                    return result_str

            try:
                return await call_tool_result(session)
            except Exception as e:
                if attempt == 0:
                    # First attempt failed, try to reconnect
                    logger.warning(
                        f"Error calling MCP tool '{name}' on server '{server_id}': {e}. "
                        f"Attempting to reconnect..."
                    )

                    if _is_connection_error(e, include_timeouts=True):
                        # Try to reconnect
                        reconnected = await self._reconnect_server(server_id)
                        if reconnected:
                            # Update session reference after reconnection
                            session = self._sessions.get(server_id)
                            if session:
                                logger.info(
                                    f"Retrying tool '{name}' after successful reconnection..."
                                )
                                continue  # Retry the tool execution
                            else:
                                logger.error(
                                    f"Session still unavailable after reconnection for '{server_id}'"
                                )
                        else:
                            logger.error(f"Failed to reconnect to server '{server_id}'")
                    else:
                        # Not a connection error, don't retry
                        logger.exception(
                            f"Non-connection error calling MCP tool '{name}': {e}"
                        )
                        return f"Error calling MCP tool '{name}': {e}"

                # If we get here, either it's the second attempt or reconnection failed
                logger.exception(
                    f"Error calling MCP tool '{name}' on server '{server_id}': {e}"
                )
                return f"Error calling MCP tool '{name}': {e}"

        # This should never be reached, but needed for type checking
        return f"Error: Unexpected execution path for tool '{name}'"

    async def _close_server_connections(self, server_id: str) -> None:
        """Close connections for a specific server."""
        # First try to close the session if it exists
        if server_id in self._sessions:
            try:
                # Just remove the session, don't try to close it as it may cause cross-task issues
                del self._sessions[server_id]
                logger.debug(f"Removed session for server '{server_id}'")
            except Exception as e:
                logger.warning(f"Error removing session for server '{server_id}': {e}")

        # Then handle the context managers with proper error handling
        if server_id in self._connection_contexts:
            exit_stack = self._connection_contexts.pop(server_id)
            try:
                await exit_stack.aclose()
                logger.debug(f"Closed contexts for server '{server_id}'")
            except RuntimeError as e:
                if "cancel scope in a different task" in str(e):
                    logger.debug(
                        f"Ignoring expected cancel scope error for server '{server_id}' during shutdown"
                    )
                else:
                    logger.warning(
                        f"RuntimeError closing context manager for server '{server_id}': {e}"
                    )
            except asyncio.CancelledError:
                logger.debug(
                    f"Context manager closure cancelled for server '{server_id}'"
                )
            except Exception as e:
                logger.warning(
                    f"Error closing context manager for server '{server_id}': {type(e).__name__}: {e}"
                )

    def get_tool_to_server_mapping(self) -> dict[str, str]:
        """Returns a mapping of tool names to their server IDs.

        Returns:
            Dictionary mapping tool name to server ID
        """
        return self._tool_map.copy()

    async def close(self) -> None:
        """Closes all managed MCP connections and cleans up resources."""
        logger.info(
            f"Closing MCPToolsProvider: Shutting down {len(self._sessions)} sessions..."
        )

        # Stop health check task
        self._health_check_enabled = False
        if self._health_check_task and not self._health_check_task.done():
            logger.info("Stopping health check task...")
            self._health_check_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._health_check_task

        # Close all server connections
        for server_id in list(self._connection_contexts.keys()):
            await self._close_server_connections(server_id)

        self._sessions.clear()
        self._tool_map.clear()
        self._definitions.clear()
        self._descriptors.clear()
        self._bump_descriptors_version()
        self._initialized = False
        logger.info("MCPToolsProvider closed.")
