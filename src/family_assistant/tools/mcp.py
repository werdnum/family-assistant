import asyncio
import logging
import os  # Import os for environment variable resolution
from contextlib import AsyncExitStack  # Import AsyncExitStack
from typing import (
    Any,
)  # Added Tuple

from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.http import sse_client  # Assuming sse_client is in mcp.http
from mcp.types import TextContent  # Import TextContent from mcp.types

# Import storage functions needed by local tools
# Import the context from the new types file
from .types import ToolExecutionContext, ToolNotFoundError

logger = logging.getLogger(__name__)


class MCPToolsProvider:
    """
    Provides and executes tools hosted on MCP servers.
    Handles connection, fetching definitions, and execution.
    """

    def __init__(
        self,
        mcp_server_configs: dict[
            str, dict[str, Any]
        ],  # Expects dict {server_id: config}
    ) -> None:
        self._mcp_server_configs = mcp_server_configs
        self._sessions: dict[str, ClientSession] = {}
        self._tool_map: dict[str, str] = {}  # Map tool name -> server_id
        self._definitions: list[dict[str, Any]] = []
        self._initialized = False
        self._exit_stack = AsyncExitStack()  # Manage stdio process lifecycles
        logger.info(
            f"MCPToolsProvider created for {len(self._mcp_server_configs)} configured servers. Initialization pending."
        )

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
        all_tool_names = set()  # To detect duplicates across servers

        async def _connect_and_discover_mcp(
            server_id: str, server_conf: dict[str, Any]
        ) -> tuple["ClientSession | None", list[dict[str, Any]], dict[str, str]]:
            """Connects to a single MCP server, discovers tools, and returns results."""
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
                        resolved_env_stdio[key] = (
                            value  # Fix typo: use resolved_env_stdio
                        )
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
                        return None, [], {}
                    server_params = StdioServerParameters(
                        command=command,
                        args=args,
                        env=resolved_env_stdio,  # Use stdio-specific env vars
                    )
                    # Use the provider's exit stack to manage stdio process context
                    (
                        read_stream,
                        write_stream,
                    ) = await self._exit_stack.enter_async_context(
                        stdio_client(server_params)
                    )
                    # Create session with streams, manage session lifecycle with exit stack
                    session = await self._exit_stack.enter_async_context(
                        ClientSession(read_stream, write_stream)
                    )
                elif transport_type == "sse":
                    if not url:
                        logger.error(
                            f"MCP server '{server_id}' (sse): 'url' is missing."
                        )
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

                    # Use the sse_client context manager via the exit stack
                    # to get the streams and manage the connection lifecycle.
                    # Pass url and headers. Use default timeouts for now.
                    (
                        read_stream,
                        write_stream,
                    ) = await self._exit_stack.enter_async_context(
                        sse_client(url=url, headers=headers)
                    )
                    # Create session with the streams obtained from the sse_client context
                    session = await self._exit_stack.enter_async_context(
                        ClientSession(read_stream, write_stream)
                    )
                else:
                    logger.error(
                        f"Unsupported transport type '{transport_type}' for MCP server '{server_id}'."
                    )
                    return None, [], {}

                # --- Initialize Session and Discover Tools (Common Logic) ---
                await session.initialize()
                logger.info(
                    f"Initialized session with MCP server '{server_id}' ({transport_type})."
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
                        if tool_name in all_tool_names:
                            logger.warning(
                                f"Duplicate tool name '{tool_name}' found on server '{server_id}'. It will be ignored from this server. Previous source: '{self._tool_map.get(tool_name)}'."
                            )
                        else:
                            tool_map[tool_name] = (
                                server_id  # Map name to server_id for this task's result
                            )
                            all_tool_names.add(
                                tool_name
                            )  # Add to overall set for duplicate check
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
                return None, [], {}  # Return empty on failure

        # --- Create connection tasks ---
        connection_tasks = [
            _connect_and_discover_mcp(server_id, server_conf)
            for server_id, server_conf in self._mcp_server_configs.items()
        ]

        # --- Run tasks concurrently ---
        logger.info(
            f"Starting parallel connection to {len(connection_tasks)} MCP server(s)..."
        )
        # Add explicit type hint for results to help type checker
        results: list[Any] = await asyncio.gather(
            *connection_tasks, return_exceptions=True
        )
        logger.info("Finished parallel MCP connection attempts.")

        # --- Process results ---
        for i, res_item in enumerate(results):
            server_id = list(self._mcp_server_configs.keys())[i]

            if isinstance(res_item, BaseException):
                logger.error(
                    f"Gather caught exception for server '{server_id}': {res_item}"
                )
            elif res_item is None:
                # This case should ideally not be hit if _connect_and_discover_mcp always returns a tuple,
                # even on failure (e.g., (None, [], {})).
                logger.warning(
                    f"Received None result for server '{server_id}' from task."
                )
            else:
                # Expect res_item to be a tuple: (ClientSession | None, list[dict], dict)
                session, discovered_tools, tool_map_for_server = res_item
                if session:
                    self._sessions[server_id] = session
                    self._definitions.extend(discovered_tools)
                    self._tool_map.update(tool_map_for_server)
                else:
                    # This means _connect_and_discover_mcp returned, but no session was established.
                    logger.warning(
                        f"Connection/discovery for MCP server '{server_id}' completed but yielded no active session. Result: {res_item}"
                    )

        self._initialized = True
        logger.info(
            f"MCPToolsProvider initialization complete. Active sessions: {len(self._sessions)}. Mapped {len(self._tool_map)} unique tools from {len(self._definitions)} total definitions."
        )

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

    async def execute_tool(
        self, name: str, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> str:
        """Executes an MCP tool on the appropriate server."""
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
            logger.error(
                f"Error calling MCP tool '{name}' on server '{server_id}': {e}",
                exc_info=True,
            )
            return f"Error calling MCP tool '{name}': {e}"

    async def close(self) -> None:
        """Closes all managed MCP connections and cleans up resources."""
        logger.info(
            f"Closing MCPToolsProvider: Shutting down {len(self._sessions)} sessions..."
        )
        await self._exit_stack.aclose()
        self._sessions.clear()
        self._tool_map.clear()
        self._definitions.clear()
        self._initialized = False
        logger.info("MCPToolsProvider closed.")
