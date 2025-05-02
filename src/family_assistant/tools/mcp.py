import asyncio
import json
import logging
import uuid
import inspect
import zoneinfo
from dataclasses import dataclass
from datetime import datetime, timezone, date, time
from typing import List, Dict, Any, Optional, Protocol, Callable, Awaitable, Set
from zoneinfo import ZoneInfo

from contextlib import AsyncExitStack # Import AsyncExitStack
import os # Import os for environment variable resolution
from dateutil import rrule
from dateutil.parser import isoparse
from mcp import ClientSession, ServerDetails, StdioServerParameters # Import necessary MCP classes (Removed Client)
from mcp.client.stdio import stdio_client # Import stdio_client
from telegram.ext import Application
from sqlalchemy.sql import text

# Import storage functions needed by local tools
from family_assistant import storage
from family_assistant.storage import get_recent_history
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.vector_search import VectorSearchQuery, query_vector_store
from family_assistant.embeddings import EmbeddingGenerator
from datetime import timedelta

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
        mcp_server_configs: Dict[str, Dict[str, Any]], # Expects dict {server_id: config}
        mcp_client: Optional[Client] = None, # Optional pre-configured MCP Client (not used for stdio)
    ):
        self._mcp_server_configs = mcp_server_configs
        # self._mcp_client = mcp_client # Client not directly used for stdio connections
        self._sessions: Dict[str, ClientSession] = {}
        self._tool_map: Dict[str, str] = {} # Map tool name -> server_id
        self._definitions: List[Dict[str, Any]] = []
        self._initialized = False
        self._exit_stack = AsyncExitStack() # Manage stdio process lifecycles
        logger.info(
            f"MCPToolsProvider created for {len(self._mcp_server_configs)} configured servers. Initialization pending."
        )

    async def initialize(self):
        """Connects to configured MCP servers, fetches and sanitizes tool definitions."""
        if self._initialized:
            return

        logger.info(f"Initializing MCPToolsProvider: Connecting to {len(self._mcp_server_configs)} servers...")
        self._sessions = {}
        self._tool_map = {}
        self._definitions = []
        all_tool_names = set() # To detect duplicates across servers

        async def _connect_and_discover_mcp(
            server_id: str, server_conf: Dict[str, Any]
        ) -> Tuple[Optional[ClientSession], List[Dict[str, Any]], Dict[str, str]]:
            """Connects to a single MCP server, discovers tools, and returns results."""
            discovered_tools = []
            tool_map = {}
            session = None

            command = server_conf.get("command")
            args = server_conf.get("args", [])
            env_config = server_conf.get("env")  # Original env config from JSON

            # --- Resolve environment variable placeholders ---
            resolved_env = None
            if isinstance(env_config, dict):
                resolved_env = {}
                for key, value in env_config.items():
                    if isinstance(value, str) and value.startswith("$"):
                        env_var_name = value[1:]  # Remove the leading '$'
                        resolved_value = os.getenv(env_var_name)
                        if resolved_value is not None:
                            resolved_env[key] = resolved_value
                            logger.debug(
                                f"Resolved env var '{env_var_name}' for MCP server '{server_id}'"
                            )
                        else:
                            logger.warning(
                                f"Env var '{env_var_name}' for MCP server '{server_id}' not found in environment. Omitting."
                            )
                    else:
                        resolved_env[key] = value
            elif env_config is not None:
                logger.warning(
                    f"MCP server '{server_id}' has non-dictionary 'env' configuration. Ignoring."
                )
            # --- End environment variable resolution ---

            if not command:
                logger.error(f"MCP server '{server_id}': 'command' is missing.")
                return None, [], {}

            logger.info(
                f"Attempting connection and discovery for MCP server '{server_id}'..."
            )
            try:
                server_params = StdioServerParameters(
                    command=command, args=args, env=resolved_env
                )
                # Use the provider's exit stack to manage contexts
                read_stream, write_stream = await self._exit_stack.enter_async_context(
                    stdio_client(server_params)
                )
                session = await self._exit_stack.enter_async_context(
                    ClientSession(read_stream, write_stream)
                )
                await session.initialize()
                logger.info(f"Initialized session with MCP server '{server_id}'.")

                response = await session.list_tools()
                server_tools = response.tools
                logger.info(f"Server '{server_id}' provides {len(server_tools)} tools.")

                # Sanitize and map tools
                sanitized_tools = self._sanitize_mcp_definitions(server_tools) # Sanitize here
                discovered_tools.extend(sanitized_tools)

                for tool_def in sanitized_tools: # Iterate sanitized definitions
                    func_def = tool_def.get("function", {})
                    tool_name = func_def.get("name")
                    if tool_name:
                        if tool_name in all_tool_names:
                            logger.warning(f"Duplicate tool name '{tool_name}' found on server '{server_id}'. It will be ignored from this server. Previous source: '{self._tool_map.get(tool_name)}'.")
                        else:
                            tool_map[tool_name] = server_id # Map name to server_id for this task's result
                            all_tool_names.add(tool_name) # Add to overall set for duplicate check
                    else:
                         logger.warning(f"Found tool definition without a name on server '{server_id}': {tool_def}")

                return session, discovered_tools, tool_map

            except Exception as e:
                logger.error(f"Failed connection/discovery for MCP server '{server_id}': {e}", exc_info=True)
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
        results = await asyncio.gather(*connection_tasks, return_exceptions=True)
        logger.info("Finished parallel MCP connection attempts.")

        # --- Process results ---
        for i, result in enumerate(results):
            server_id = list(self._mcp_server_configs.keys())[i] # Get corresponding server_id
            if isinstance(result, Exception):
                logger.error(f"Gather caught exception for server '{server_id}': {result}")
            elif result:
                session, discovered, tool_map = result
                if session:
                    self._sessions[server_id] = session  # Store successful session
                    self._definitions.extend(discovered) # Add sanitized tools
                    self._tool_map.update(tool_map) # Add mappings for this server
                else:
                    logger.warning(
                        f"Connection/discovery seems to have failed silently for server '{server_id}' (result: {result})."
                    )
            else:
                logger.warning(
                    f"Received unexpected empty result for server '{server_id}'."
                )

        self._initialized = True
        logger.info(
            f"MCPToolsProvider initialization complete. Active sessions: {len(self._sessions)}. Mapped {len(self._tool_map)} unique tools from {len(self._definitions)} total definitions."
        )


    def _sanitize_mcp_definitions(
        # self, definitions: List[Dict[str, Any]] # Original signature
        self, definitions: List[Any] # MCP list_tools returns list of Tool objects
    ) -> List[Dict[str, Any]]:
        """
        Removes unsupported 'format' fields from string parameters in tool definitions.
        Google's API only supports 'enum' and 'date-time' for string formats.
        Accepts a list of MCP Tool objects.
        """
        sanitized_defs = []
        for tool in definitions: # Iterate MCP Tool objects
            try:
                # Convert MCP Tool object to OpenAI-like dictionary format
                tool_dict = {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.inputSchema,
                    },
                }

                # Now sanitize the 'parameters' part of the dictionary
                func_def = tool_dict.get("function", {})
                params = func_def.get("parameters", {})
                properties = params.get("properties", {})

                if not isinstance(properties, dict):
                    logger.warning(f"Tool '{tool.name}' has non-dict 'properties'. Skipping sanitization for this tool.")
                    sanitized_defs.append(tool_dict) # Add unsanitized but formatted dict
                    continue

                props_to_delete_format = []
                for param_name, param_details in properties.items():
                    if isinstance(param_details, dict):
                        param_type = param_details.get("type")
                        param_format = param_details.get("format")

                        if (
                            param_type == "string"
                            and param_format
                            and param_format not in ["enum", "date-time"]
                        ):
                            logger.warning(
                                f"Sanitizing tool '{tool.name}': Removing unsupported format '{param_format}' from string parameter '{param_name}'."
                            )
                            props_to_delete_format.append(param_name)

                # Perform deletion after iteration
                for param_name in props_to_delete_format:
                    if param_name in properties and isinstance(properties[param_name], dict):
                        # Ensure 'format' key exists before deleting
                        if 'format' in properties[param_name]:
                            del properties[param_name]["format"]

                sanitized_defs.append(tool_dict) # Add the sanitized dict
            except Exception as e:
                logger.error(
                    f"Error formatting or sanitizing MCP tool definition: {tool.name}. Error: {e}",
                    exc_info=True,
                )
                # Optionally skip the tool or add a placeholder
                # sanitized_defs.append({...}) # Add placeholder if needed

        return sanitized_defs

    async def get_tool_definitions(self) -> List[Dict[str, Any]]:
        """Returns the aggregated and sanitized tool definitions from all connected servers."""
        if not self._initialized:
            await self.initialize()
        return self._definitions

    async def execute_tool(
        self, name: str, arguments: Dict[str, Any], context: ToolExecutionContext
    ) -> str:
        """Executes an MCP tool on the appropriate server."""
        if not self._initialized:
            await self.initialize() # Ensure connections and mapping are ready

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
                    if hasattr(content_item, "text") and content_item.text:
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

    async def close(self):
        """Closes all managed MCP connections and cleans up resources."""
        logger.info(f"Closing MCPToolsProvider: Shutting down {len(self._sessions)} sessions...")
        await self._exit_stack.aclose()
        self._sessions.clear()
        self._tool_map.clear()
        self._definitions.clear()
        self._initialized = False
        logger.info("MCPToolsProvider closed.")
