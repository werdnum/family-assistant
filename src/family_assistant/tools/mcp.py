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

from dateutil import rrule
from dateutil.parser import isoparse
from mcp import ClientSession
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
    """Provides and executes tools hosted on MCP servers."""

    def __init__(
        self,
        mcp_definitions: List[Dict[str, Any]],
        mcp_sessions: Dict[str, ClientSession],
        tool_name_to_server_id: Dict[str, str],
    ):
        # Sanitize definitions before storing them
        sanitized_definitions = self._sanitize_mcp_definitions(mcp_definitions)
        self._definitions = sanitized_definitions
        self._sessions = mcp_sessions
        self._tool_map = tool_name_to_server_id
        logger.info(
            f"MCPToolsProvider initialized with {len(self._definitions)} sanitized tools from {len(self._sessions)} sessions."
        )

    def _sanitize_mcp_definitions(
        self, definitions: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Removes unsupported 'format' fields from string parameters in tool definitions.
        Google's API only supports 'enum' and 'date-time' for string formats.
        """
        sanitized = []
        for tool_def in definitions:
            try:
                # Deep copy to avoid modifying original dicts if they are reused elsewhere
                # Though in this context, it might not be strictly necessary
                # sanitized_tool_def = copy.deepcopy(tool_def) # Consider adding import copy
                sanitized_tool_def = json.loads(
                    json.dumps(tool_def)
                )  # Simple deep copy via JSON

                func_def = sanitized_tool_def.get("function", {})
                params = func_def.get("parameters", {})
                properties = params.get("properties", {})

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
                                f"Sanitizing tool '{func_def.get('name', 'UNKNOWN')}': Removing unsupported format '{param_format}' from string parameter '{param_name}'."
                            )
                            # Don't modify while iterating, mark for deletion
                            props_to_delete_format.append(param_name)

                # Perform deletion after iteration
                for param_name in props_to_delete_format:
                    if param_name in properties and isinstance(
                        properties[param_name], dict
                    ):
                        del properties[param_name]["format"]

                sanitized.append(sanitized_tool_def)
            except Exception as e:
                logger.error(
                    f"Error sanitizing tool definition: {tool_def}. Error: {e}",
                    exc_info=True,
                )
                # Decide whether to skip the tool or add the original unsanitized one
                sanitized.append(tool_def)  # Add original if sanitization fails

        return sanitized

    async def get_tool_definitions(self) -> List[Dict[str, Any]]:
        # Definitions are already sanitized during init
        return self._definitions

    async def execute_tool(
        self, name: str, arguments: Dict[str, Any], context: ToolExecutionContext
    ) -> str:
        server_id = self._tool_map.get(name)
        if not server_id:
            raise ToolNotFoundError(f"MCP tool '{name}' not found in tool map.")

        session = self._sessions.get(server_id)
        if not session:
            # This case should ideally be prevented by ensuring sessions are active,
            # but handle defensively.
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
