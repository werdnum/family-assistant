import logging
import json
import asyncio
import traceback # Moved import to top level
import uuid  # Added for unique task IDs
from datetime import datetime, timedelta, timezone # Added timezone, timedelta
from typing import List, Dict, Any, Optional, Callable, Tuple, Union, Awaitable # Added Union, Awaitable

from dateutil.parser import isoparse # Added for parsing datetime strings
import pytz  # Added

# Import the LLM interface and output structure
from .llm import LLMInterface, LLMOutput
# Import ToolsProvider interface and context
from .tools import ToolsProvider, ToolExecutionContext, ToolNotFoundError
from telegram.ext import Application

# Import DatabaseContext for type hinting
from .storage.context import DatabaseContext

# Import storage and calendar integration for context building
from family_assistant import storage
from family_assistant import calendar_integration


logger = logging.getLogger(__name__)


# --- Processing Service Class ---
# Tool definitions and implementations are now moved to tools.py


class ProcessingService:
    """
    Encapsulates the logic for preparing context, processing messages,
    interacting with the LLM, and handling tool calls.
    """

    def __init__(
        self,
        llm_client: LLMInterface,
        tools_provider: ToolsProvider,
        prompts: Dict[str, str],
        calendar_config: Dict[str, Any],
        timezone_str: str,
        max_history_messages: int,
        server_url: Optional[str], # Added server_url
        history_max_age_hours: int, # Recommended value is now 1
    ):
        """
        Initializes the ProcessingService.

        Args:
            llm_client: An object implementing the LLMInterface protocol.
            tools_provider: An object implementing the ToolsProvider protocol.
            prompts: Dictionary containing loaded prompts.
            calendar_config: Dictionary containing calendar configuration.
            timezone_str: The configured timezone string (e.g., "Europe/London").
            max_history_messages: Max number of history messages to fetch.
            server_url: The base URL of the web server.
            history_max_age_hours: Max age of history messages to fetch (in hours). Recommended: 1.
        """
        self.llm_client = llm_client
        self.tools_provider = tools_provider
        self.prompts = prompts
        self.calendar_config = calendar_config
        self.timezone_str = timezone_str
        self.max_history_messages = max_history_messages
        self.server_url = server_url or "http://localhost:8000" # Default if not provided
        self.history_max_age_hours = history_max_age_hours
        # Store the confirmation callback function if provided at init? No, get from context.

    def _format_history_for_llm(
        self, history_messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Formats message history retrieved from the database into the list structure
        expected by the LLM, handling assistant tool calls correctly.

        Args:
            history_messages: List of message dictionaries from storage.get_recent_history.

        Returns:
            A list of message dictionaries formatted for the LLM API.
        """
        messages: List[Dict[str, Any]] = []
        # (Rest of the formatting logic as provided)
        logger.debug(
            f"Formatted {len(history_messages)} DB history messages into {len(messages)} LLM messages."
        )
        return messages

    async def generate_llm_response_for_chat(
        self,
