import asyncio
import contextlib  # Added contextlib
import logging
import os  # Import os for environment variable resolution
from typing import (
    Any,
)  # Added Tuple

from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.sse import sse_client  # Assuming sse_client is in mcp.client.sse
from mcp.types import TextContent  # Import TextContent from mcp.types

# Import storage functions needed by local tools
# Import the context from the new types file
from .types import ToolExecutionContext, ToolNotFoundError

logger = logging.getLogger(__name__)

# MCP Server Status Constants
MCP_SERVER_STATUS_PENDING = "pending"
MCP_SERVER_STATUS_CONNECTING = "connecting"
MCP_SERVER_STATUS_CONNECTED = "connected"
MCP_SERVER_STATUS_FAILED = "failed"
MCP_SERVER_STATUS_CANCELLED = "cancelled"


class MCPToolsProvider:
    """
    Provides and executes tools hosted on MCP servers.
    Handles connection, fetching definitions, and execution.
    """

    def __init__(
        self,
        mcp_server_configs: dict[str, dict[str, Any]],
        initialization_timeout_seconds: int = 60,  # Default 1 minute
        health_check_interval_seconds: int = 30,  # Default 30 seconds
    ) -> None:
        self._mcp_server_configs = mcp_server_configs
        self._initialization_timeout_seconds = initialization_timeout_seconds
        self._health_check_interval_seconds = health_check_interval_seconds
        self._sessions: dict[str, ClientSession] = {}
        self._tool_map: dict[str, str] = {}  # Map tool name -> server_id
        self._definitions: list[dict[str, Any]] = []
        self._initialized = False
        # Store the context managers directly instead of using AsyncExitStack
        self._connection_contexts: dict[str, list[Any]] = {}
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
            f"Initialization pending."
        )

    async def _log_mcp_initialization_progress(
        self, stop_event: asyncio.Event, start_time: float
    ) -> None:
        """Logs progress during MCP tool initialization."""
        logger.debug("MCP initialization logging task started.")
        try:
            while not stop_event.is_set():
                try:
                    # Wait for 10 seconds or until stop_event is set
                    await asyncio.wait_for(stop_event.wait(), timeout=10.0)
                except asyncio.TimeoutError:
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
                            in [MCP_SERVER_STATUS_PENDING, MCP_SERVER_STATUS_CONNECTING]
                        ]
                        logger.info(
                            f"Still initializing MCP tools... "
                            f"Waiting for {len(pending_servers)} of {len(self._mcp_server_configs)} server(s): {', '.join(pending_servers) if pending_servers else 'None'}. "
                            f"Elapsed: {elapsed_time:.0f}s. "
                            f"Timeout in approx {max(0, remaining_time):.0f}s (total {self._initialization_timeout_seconds}s)."
                        )
                except asyncio.CancelledError:  # If logging_task itself is cancelled
                    raise
        except asyncio.CancelledError:
            logger.debug("MCP initialization logging task cancelled.")
        except Exception as e:
            logger.error(
                f"Unexpected error in MCP initialization logging task: {e}",
                exc_info=True,
            )
        finally:
            logger.debug("MCP initialization logging task finished.")

    async def _connect_and_discover_mcp(
        self, server_id: str, server_conf: dict[str, Any]
    ) -> tuple["ClientSession | None", list[dict[str, Any]], dict[str, str]]:
        """Connects to a single MCP server, discovers tools, and returns results."""
        self._server_statuses[server_id] = MCP_SERVER_STATUS_CONNECTING
        discovered_tools = []
        tool_map = {}
        session = None

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
        try:
            # --- Transport and Session Creation ---
            if transport_type == "stdio":
                if not command:
                    logger.error(
                        f"MCP server '{server_id}' (stdio): 'command' is missing."
                    )
                    self._server_statuses[server_id] = MCP_SERVER_STATUS_FAILED
                    return None, [], {}
                server_params = StdioServerParameters(
                    command=command,
                    args=args,
                    env=resolved_env_stdio,  # Use stdio-specific env vars
                )

                # Create the stdio client context manager
                stdio_cm = stdio_client(server_params)
                read_stream, write_stream = await stdio_cm.__aenter__()  # pylint: disable=no-member

                # Create the session context manager
                session_cm = ClientSession(read_stream, write_stream)
                session = await session_cm.__aenter__()

                # Store the context managers for cleanup
                self._connection_contexts[server_id] = [stdio_cm, session_cm]

            elif transport_type == "sse":
                if not url:
                    logger.error(f"MCP server '{server_id}' (sse): 'url' is missing.")
                    self._server_statuses[server_id] = MCP_SERVER_STATUS_FAILED
                    return None, [], {}

                # Construct headers using the resolved token
                headers = {}
                if resolved_token_sse:
                    headers["Authorization"] = f"Bearer {resolved_token_sse}"
                    logger.debug(
                        f"Using Authorization header for SSE server '{server_id}'."
                    )
                else:
                    logger.warning(
                        f"No token resolved for SSE server '{server_id}'. Connecting without Authorization header."
                    )
                    # Add other potential header mappings here if needed

                # Create the SSE client context manager
                sse_cm = sse_client(url=url, headers=headers)
                read_stream, write_stream = await sse_cm.__aenter__()  # pylint: disable=no-member

                # Create the session context manager
                session_cm = ClientSession(read_stream, write_stream)
                session = await session_cm.__aenter__()

                # Store the context managers for cleanup
                self._connection_contexts[server_id] = [sse_cm, session_cm]

            else:
                logger.error(
                    f"Unsupported transport type '{transport_type}' for MCP server '{server_id}'."
                )
                self._server_statuses[server_id] = MCP_SERVER_STATUS_FAILED
                return None, [], {}

            # --- Initialize Session and Discover Tools (Common Logic) ---
            await session.initialize()
            self._server_statuses[server_id] = MCP_SERVER_STATUS_CONNECTED
            logger.info(
                f"Initialized session with MCP server '{server_id}' ({transport_type}). Status: {self._server_statuses[server_id]}."
            )

            response = await session.list_tools()
            server_tools = response.tools
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

            return session, discovered_tools, tool_map

        except Exception as e:
            logger.error(
                f"Failed connection/discovery for MCP server '{server_id}': {e}",
                exc_info=True,
            )
            self._server_statuses[server_id] = MCP_SERVER_STATUS_FAILED
            # Clean up any partially created contexts
            if server_id in self._connection_contexts:
                await self._close_server_connections(server_id)
            return None, [], {}  # Return empty on failure

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
        # Reset server statuses to PENDING if re-initializing
        self._server_statuses = {
            sid: MCP_SERVER_STATUS_PENDING for sid in self._mcp_server_configs
        }
        all_tool_names = set()  # To detect duplicates across servers

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
        except asyncio.TimeoutError:
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
            logger.error(
                f"Unexpected error during MCP connection gathering: {e}", exc_info=True
            )
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
                except asyncio.TimeoutError:
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
                    if self._server_statuses[server_id] not in [
                        MCP_SERVER_STATUS_FAILED,
                        MCP_SERVER_STATUS_CANCELLED,
                    ]:
                        self._server_statuses[server_id] = MCP_SERVER_STATUS_FAILED
            elif res_item is None:
                logger.warning(
                    f"Received None result for server '{server_id}' from task (should be tuple)."
                )
                self._server_statuses[server_id] = MCP_SERVER_STATUS_FAILED
            else:
                session, discovered_tools, tool_map_for_server = res_item
                if session:
                    # Status should be CONNECTED from _connect_and_discover_mcp
                    self._sessions[server_id] = session

                    # Check for duplicates before adding
                    for tool_def in discovered_tools:
                        func_def = tool_def.get("function", {})
                        tool_name = func_def.get("name")
                        if tool_name:
                            if tool_name in all_tool_names:
                                logger.warning(
                                    f"Duplicate tool name '{tool_name}' found on server '{server_id}'. "
                                    f"It will be ignored from this server. Previous source: '{self._tool_map.get(tool_name)}'."
                                )
                                # Remove from tool_map_for_server to prevent overwriting
                                tool_map_for_server.pop(tool_name, None)
                            else:
                                all_tool_names.add(tool_name)
                                self._definitions.append(tool_def)

                    self._tool_map.update(tool_map_for_server)
                else:
                    logger.warning(
                        f"Connection/discovery for MCP server '{server_id}' completed but yielded no active session. Result: {res_item}"
                    )
                    if self._server_statuses[server_id] == MCP_SERVER_STATUS_CONNECTING:
                        # If it was connecting but no session, mark failed
                        self._server_statuses[server_id] = MCP_SERVER_STATUS_FAILED

        self._initialized = True
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

        # Start health check task if we have any connected sessions
        if self._sessions and self._health_check_enabled:
            self._health_check_task = asyncio.create_task(self._health_check_loop())
            logger.info("Started MCP server health check task")

    def _format_mcp_definitions_to_dicts(
        # self, definitions: List[Dict[str, Any]] # Original signature
        self,
        definitions: list[Any],  # MCP list_tools returns list of Tool objects
    ) -> list[dict[str, Any]]:
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
                logger.error(
                    f"Error formatting MCP tool definition to dict: {getattr(tool, 'name', 'UnknownName')}. Error: {e}",
                    exc_info=True,
                )

        return formatted_defs

    async def get_tool_definitions(
        self,
    ) -> list[dict[str, Any]]:  # Return type is still dict
        """Returns the aggregated and sanitized tool definitions from all connected servers."""
        if not self._initialized:
            await self.initialize()
        return self._definitions

    async def _health_check_loop(self) -> None:
        """Periodically checks the health of connected MCP servers."""
        logger.info(
            f"Starting health check loop with interval {self._health_check_interval_seconds}s"
        )

        while self._health_check_enabled:
            try:
                # Wait for the interval
                await asyncio.sleep(self._health_check_interval_seconds)

                if not self._health_check_enabled:
                    break

                # Check each connected server
                for server_id, session in list(self._sessions.items()):
                    if not self._health_check_enabled:
                        break

                    try:
                        # Simple health check - list tools to verify connection
                        # Using a short timeout to avoid blocking too long
                        await asyncio.wait_for(session.list_tools(), timeout=5.0)
                        logger.debug(f"Health check passed for server '{server_id}'")

                    except asyncio.TimeoutError:
                        logger.warning(f"Health check timeout for server '{server_id}'")
                        # Don't reconnect on timeout - server might just be slow

                    except Exception as e:
                        logger.warning(
                            f"Health check failed for server '{server_id}': {e}"
                        )

                        # Check if it's a connection error
                        error_str = str(e).lower()
                        is_connection_error = any(
                            phrase in error_str
                            for phrase in [
                                "connection",
                                "closed",
                                "reset",
                                "broken pipe",
                                "eof",
                                "disconnected",
                                "not connected",
                            ]
                        )

                        if is_connection_error:
                            logger.info(
                                f"Detected connection issue for server '{server_id}', attempting reconnection..."
                            )
                            self._server_statuses[server_id] = MCP_SERVER_STATUS_FAILED

                            # Try to reconnect
                            reconnected = await self._reconnect_server(server_id)
                            if reconnected:
                                logger.info(
                                    f"Successfully reconnected server '{server_id}' during health check"
                                )
                            else:
                                logger.error(
                                    f"Failed to reconnect server '{server_id}' during health check"
                                )

            except asyncio.CancelledError:
                logger.info("Health check loop cancelled")
                break
            except Exception as e:
                logger.error(
                    f"Unexpected error in health check loop: {e}", exc_info=True
                )
                # Continue the loop despite errors

        logger.info("Health check loop stopped")

    async def _reconnect_server(self, server_id: str) -> bool:
        """Attempts to reconnect a single MCP server."""
        logger.info(f"Attempting to reconnect MCP server '{server_id}'...")

        # Get the server config
        server_conf = self._mcp_server_configs.get(server_id)
        if not server_conf:
            logger.error(f"No configuration found for server '{server_id}'")
            return False

        # Close existing session and connection if any
        if server_id in self._sessions:
            try:
                # Remove from sessions to prevent reuse during reconnection
                self._sessions.pop(server_id)
                # Close the context managers for this server
                await self._close_server_connections(server_id)
            except Exception as e:
                logger.warning(f"Error removing old session for '{server_id}': {e}")

        # Remove tools from this server from the tool map
        tools_to_remove = [
            name for name, sid in self._tool_map.items() if sid == server_id
        ]
        for tool_name in tools_to_remove:
            del self._tool_map[tool_name]

        # Remove definitions from this server
        self._definitions = [
            d
            for d in self._definitions
            if d.get("function", {}).get("name") not in tools_to_remove
        ]

        # Attempt reconnection
        try:
            # Call the existing connection method
            session, discovered_tools, tool_map = await self._connect_and_discover_mcp(
                server_id, server_conf
            )

            if session:
                self._sessions[server_id] = session
                self._definitions.extend(discovered_tools)
                self._tool_map.update(tool_map)
                logger.info(
                    f"Successfully reconnected MCP server '{server_id}' with {len(discovered_tools)} tools"
                )
                return True
            else:
                logger.error(f"Failed to reconnect MCP server '{server_id}'")
                return False

        except Exception as e:
            logger.error(
                f"Error reconnecting MCP server '{server_id}': {e}", exc_info=True
            )
            self._server_statuses[server_id] = MCP_SERVER_STATUS_FAILED
            return False

    async def execute_tool(
        self, name: str, arguments: dict[str, Any], context: ToolExecutionContext
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
            try:
                mcp_result = await session.call_tool(name=name, arguments=arguments)

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

            except Exception as e:
                if attempt == 0:
                    # First attempt failed, try to reconnect
                    logger.warning(
                        f"Error calling MCP tool '{name}' on server '{server_id}': {e}. "
                        f"Attempting to reconnect..."
                    )

                    # Check if this looks like a connection error
                    error_str = str(e).lower()
                    is_connection_error = any(
                        phrase in error_str
                        for phrase in [
                            "connection",
                            "closed",
                            "reset",
                            "broken pipe",
                            "eof",
                            "timeout",
                            "disconnected",
                            "not connected",
                        ]
                    )

                    if is_connection_error:
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
                        logger.error(
                            f"Non-connection error calling MCP tool '{name}': {e}",
                            exc_info=True,
                        )
                        return f"Error calling MCP tool '{name}': {e}"

                # If we get here, either it's the second attempt or reconnection failed
                logger.error(
                    f"Error calling MCP tool '{name}' on server '{server_id}': {e}",
                    exc_info=True,
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
            contexts = self._connection_contexts[server_id]
            # Try to close gracefully, but handle cross-task issues
            for i, cm in enumerate(reversed(contexts)):
                try:
                    await cm.__aexit__(None, None, None)
                    logger.debug(f"Closed context manager {i} for server '{server_id}'")
                except RuntimeError as e:
                    # Handle the specific error about cancel scope in different task
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
            del self._connection_contexts[server_id]

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
        self._initialized = False
        logger.info("MCPToolsProvider closed.")
