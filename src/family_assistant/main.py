import argparse
import asyncio
import contextlib
import html
import argparse
import argparse
import asyncio
import contextlib
import html
# import io # Moved to telegram_bot.py
import json
import logging
# import base64 # Moved to telegram_bot.py
import os
import signal
import sys
import traceback # Keep for error handler if needed elsewhere, or remove if only used in bot's handler
import uuid
import yaml
import mcp  # Import MCP
from mcp import ClientSession, StdioServerParameters  # MCP specifics
from mcp.client.stdio import stdio_client  # MCP stdio client
from contextlib import AsyncExitStack  # For managing multiple async contexts
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Tuple  # Added Tuple

import pytz  # Added for timezone handling
from dotenv import load_dotenv
from telegram import Update # Keep Update if used elsewhere
# from telegram.constants import ChatAction, ParseMode # Moved to telegram_bot.py
from telegram.ext import (
    Application,
    ApplicationBuilder,
    # CallbackContext, # Moved to telegram_bot.py
    # CommandHandler, # Moved to telegram_bot.py
    # ContextTypes, # Moved to telegram_bot.py
    # MessageHandler, # Moved to telegram_bot.py
    # filters, # Moved to telegram_bot.py
)
# import telegramify_markdown # Moved to telegram_bot.py
# from telegram.helpers import escape_markdown # Moved to telegram_bot.py
import uvicorn

# Import task worker module using absolute path
from family_assistant import task_worker

# Import the ProcessingService and LLM interface/clients
from family_assistant.processing import ProcessingService
from family_assistant.llm import (
    LLMInterface,
    LiteLLMClient,
    RecordingLLMClient,
    PlaybackLLMClient,
)

# Import tool definitions from the new tools module
from family_assistant.tools import (
    TOOLS_DEFINITION as local_tools_definition,
    AVAILABLE_FUNCTIONS as local_tool_implementations,
    LocalToolsProvider,
    MCPToolsProvider,
    CompositeToolsProvider,
    ToolExecutionContext,
    ToolsProvider,  # Import protocol for type hinting
)

# Import the FastAPI app
from family_assistant.web_server import app as fastapi_app

# Import storage functions
# Import facade for primary access
from family_assistant.storage import (
    init_db,
    get_all_notes,
    add_message_to_history,
    init_db,
    # get_all_notes, # Will be called with context
    # add_message_to_history, # Will be called with context
    # get_recent_history, # Will be called with context
    # get_message_by_id, # Will be called with context
    # add_or_update_note, # Called via tools provider
)

# Import the whole storage module for task queue functions etc.
from family_assistant import storage
from family_assistant.storage.context import DatabaseContext, get_db_context # Import DatabaseContext and getter

# Import calendar functions
from family_assistant import calendar_integration

# --- Logging Configuration ---
# Set root logger level back to INFO
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# Keep external libraries less verbose unless needed
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)
logging.getLogger("apscheduler").setLevel(logging.INFO)
logging.getLogger("caldav").setLevel(
    logging.INFO
)  # Keep caldav at INFO unless specific issues arise
logger = logging.getLogger(__name__)

# --- Constants ---
MAX_HISTORY_MESSAGES = 5  # Number of recent messages to include (excluding current)
HISTORY_MAX_AGE_HOURS = 24  # Only include messages from the last X hours

# Use task_worker's events for coordination
shutdown_event = task_worker.shutdown_event
new_task_event = task_worker.new_task_event

# --- Global Variables ---
application: Optional[Application] = None
ALLOWED_CHAT_IDS: list[int] = []
DEVELOPER_CHAT_ID: Optional[int] = None
PROMPTS: Dict[str, str] = {}  # Global dict to hold loaded prompts
CALENDAR_CONFIG: Dict[str, Any] = {}  # Stores CalDAV and iCal settings
TIMEZONE_STR: str = "UTC"  # Default timezone
# shutdown_event moved higher up
# from collections import defaultdict # Moved to telegram_bot.py

mcp_sessions: Dict[str, ClientSession] = (
    {}
)  # Stores active MCP client sessions {server_id: session}
# --- State for message batching (Moved to TelegramBotHandler) ---
# chat_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
# message_buffers: Dict[int, List[Tuple[Update, Optional[bytes]]]] = defaultdict(list)
# processing_tasks: Dict[int, asyncio.Task] = {}
mcp_tools: List[Dict[str, Any]] = []  # Stores discovered MCP tools in OpenAI format
tool_name_to_server_id: Dict[str, str] = {}  # Maps MCP tool names to their server_id
mcp_exit_stack = AsyncExitStack()  # Manages MCP server process lifecycles


# Use task worker's handler registry
TASK_HANDLERS = task_worker.get_task_handlers()


logger.info(f"Available task handlers: {list(TASK_HANDLERS.keys())}")


# Use task_worker's helper function
format_llm_response_for_telegram = task_worker.format_llm_response_for_telegram


# Task worker loop is now in task_worker.py


# --- Configuration Loading ---
def load_config():
    """Loads configuration from environment variables and prompts.yaml."""
    global ALLOWED_CHAT_IDS, DEVELOPER_CHAT_ID, PROMPTS, CALENDAR_CONFIG, TIMEZONE_STR  # Added TIMEZONE_STR
    load_dotenv()  # Load environment variables from .env file

    # --- Telegram Config ---
    chat_ids_str = os.getenv("ALLOWED_CHAT_IDS", "")
    if chat_ids_str:
        try:
            ALLOWED_CHAT_IDS = [
                int(cid.strip()) for cid in chat_ids_str.split(",") if cid.strip()
            ]
            logger.info(f"Loaded {len(ALLOWED_CHAT_IDS)} allowed chat IDs.")
        except ValueError:
            logger.error(
                "Invalid format for ALLOWED_CHAT_IDS in .env file. Should be comma-separated integers."
            )
            ALLOWED_CHAT_IDS = []
    else:
        logger.warning("ALLOWED_CHAT_IDS not set. Bot will respond in all chats.")
        ALLOWED_CHAT_IDS = []

    dev_chat_id_str = os.getenv("DEVELOPER_CHAT_ID")
    if dev_chat_id_str:
        try:
            DEVELOPER_CHAT_ID = int(dev_chat_id_str)
            logger.info(f"Developer chat ID set to {DEVELOPER_CHAT_ID}.")
        except ValueError:
            logger.error("Invalid DEVELOPER_CHAT_ID in .env file. Must be an integer.")
            DEVELOPER_CHAT_ID = None
    else:
        logger.warning(
            "DEVELOPER_CHAT_ID not set. Error notifications will not be sent."
        )
        DEVELOPER_CHAT_ID = None

    # Load prompts from YAML file
    try:
        with open("prompts.yaml", "r", encoding="utf-8") as f:
            loaded_prompts = yaml.safe_load(f)
            if isinstance(loaded_prompts, dict):
                PROMPTS = loaded_prompts
                logger.info("Successfully loaded prompts from prompts.yaml")
            else:
                logger.error(
                    "Failed to load prompts: prompts.yaml is not a valid dictionary."
                )
                PROMPTS = {}  # Reset to empty if loading fails
    except FileNotFoundError:
        logger.error("prompts.yaml not found. Using default prompt structures.")
        PROMPTS = {}  # Ensure PROMPTS is initialized
    except yaml.YAMLError as e:
        logger.error(f"Error parsing prompts.yaml: {e}")
        PROMPTS = {}  # Reset to empty on parsing error

    # --- Calendar Config (CalDAV & iCal) ---
    CALENDAR_CONFIG = {}  # Initialize the combined config dict
    caldav_enabled = False
    ical_enabled = False

    # CalDAV settings
    caldav_user = os.getenv("CALDAV_USERNAME")
    caldav_pass = os.getenv("CALDAV_PASSWORD")
    caldav_urls_str = os.getenv("CALDAV_CALENDAR_URLS")
    caldav_urls = (
        [url.strip() for url in caldav_urls_str.split(",")] if caldav_urls_str else []
    )

    if caldav_user and caldav_pass and caldav_urls:
        CALENDAR_CONFIG["caldav"] = {
            "username": caldav_user,
            "password": caldav_pass,
            "calendar_urls": caldav_urls,
        }
        caldav_enabled = True
        logger.info(
            f"Loaded CalDAV configuration for {len(caldav_urls)} specific calendar URL(s)."
        )
    else:
        logger.info(
            "CalDAV configuration incomplete or disabled (requires USERNAME, PASSWORD, CALENDAR_URLS)."
        )

    # iCal settings
    ical_urls_str = os.getenv("ICAL_URLS")
    ical_urls = (
        [url.strip() for url in ical_urls_str.split(",")] if ical_urls_str else []
    )

    if ical_urls:
        CALENDAR_CONFIG["ical"] = {
            "urls": ical_urls,
        }
        ical_enabled = True
        logger.info(f"Loaded iCal configuration for {len(ical_urls)} URL(s).")
    else:
        logger.info("iCal configuration incomplete or disabled (requires ICAL_URLS).")

    if not caldav_enabled and not ical_enabled:
        logger.warning(
            "No calendar sources (CalDAV or iCal) are configured. Calendar features will be disabled."
        )
        CALENDAR_CONFIG = {}  # Ensure it's empty if nothing is enabled

    # --- Timezone Config ---
    loaded_tz = os.getenv("TIMEZONE", "UTC")
    try:
        # Validate the timezone string using pytz
        pytz.timezone(loaded_tz)
        TIMEZONE_STR = loaded_tz
        logger.info(f"Using timezone: {TIMEZONE_STR}")
    except pytz.exceptions.UnknownTimeZoneError:
        logger.error(
            f"Invalid TIMEZONE '{loaded_tz}' specified in .env. Defaulting to UTC."
        )
        TIMEZONE_STR = "UTC"  # Keep the default if invalid


# --- MCP Configuration Loading & Connection ---
async def load_mcp_config_and_connect():
    """Loads MCP server config, connects to servers, and discovers tools."""
    global mcp_sessions, mcp_tools, tool_name_to_server_id, mcp_exit_stack
    config_path = "mcp_config.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        logger.info(f"{config_path} not found. Skipping MCP server connections.")
        return
    except json.JSONDecodeError as e:
        logger.error(
            f"Error decoding {config_path}: {e}. Skipping MCP server connections."
        )
        return

    mcp_server_configs = config.get("mcpServers", {})
    if not mcp_server_configs:
        logger.info("No servers defined in mcpServers section of mcp_config.json.")
        return

    logger.info(f"Found {len(mcp_server_configs)} MCP server configurations.")

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
                        # Optionally, keep the placeholder or raise an error
                        # resolved_env[key] = value # Keep placeholder if preferred
                else:
                    # Keep non-placeholder values as is
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
            # Use the *global* exit stack to manage contexts
            read_stream, write_stream = await mcp_exit_stack.enter_async_context(
                stdio_client(server_params)
            )
            session = await mcp_exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()
            logger.info(f"Initialized session with MCP server '{server_id}'.")

            response = await session.list_tools()
            server_tools = response.tools
            logger.info(f"Server '{server_id}' provides {len(server_tools)} tools.")
            for tool in server_tools:
                discovered_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.inputSchema,  # Assuming MCP schema is compatible
                            # 'required' field might be nested differently, adjust if needed based on MCP spec
                        },
                    }
                )
                tool_map[tool.name] = server_id
                logger.info(f" -> Discovered tool: {tool.name}")
            return session, discovered_tools, tool_map

        except Exception as e:
            logger.error(f"Failed for MCP server '{server_id}': {e}", exc_info=True)
            return None, [], {}  # Return empty on failure

    # --- Create connection tasks ---
    connection_tasks = [
        _connect_and_discover_mcp(server_id, server_conf)
        for server_id, server_conf in mcp_server_configs.items()
    ]

    # --- Run tasks concurrently ---
    logger.info(
        f"Starting parallel connection to {len(connection_tasks)} MCP server(s)..."
    )
    results = await asyncio.gather(*connection_tasks, return_exceptions=True)
    logger.info("Finished parallel MCP connection attempts.")

    # --- Process results ---
    for i, result in enumerate(results):
        server_id = list(mcp_server_configs.keys())[i]  # Get corresponding server_id
        if isinstance(result, Exception):
            logger.error(f"Gather caught exception for server '{server_id}': {result}")
        elif result:
            session, discovered, tool_map = result
            if session:
                mcp_sessions[server_id] = session  # Store successful session
                mcp_tools.extend(discovered)
                tool_name_to_server_id.update(tool_map)
            else:
                logger.warning(
                    f"Connection/discovery seems to have failed silently for server '{server_id}' (result: {result})."
                )
        else:
            logger.warning(
                f"Received unexpected empty result for server '{server_id}'."
            )

    logger.info(
        f"Finished MCP setup. Active sessions: {len(mcp_sessions)}. Total discovered tools: {len(mcp_tools)}"
    )


# --- Argument Parsing ---
parser = argparse.ArgumentParser(description="Family Assistant Bot")
parser.add_argument(
    "--telegram-token",
    default=os.getenv("TELEGRAM_BOT_TOKEN"),
    help="Telegram Bot Token (overrides .env)",
)
parser.add_argument(
    "--openrouter-api-key",
    default=os.getenv("OPENROUTER_API_KEY"),
    help="OpenRouter API Key (overrides .env)",
)
parser.add_argument(
    "--model",
    default="openrouter/google/gemini-2.5-pro-preview-03-25",
    help="LLM model to use via OpenRouter (e.g., openrouter/google/gemini-2.5-pro-preview-03-25)",
)
# --- Initial Configuration Load ---
# Load config from .env and prompts.yaml first
load_config()

# Argument parsing will happen inside main()


# --- Helper Functions & Context Managers ---
# typing_notifications moved to TelegramBotHandler


# --- Core LLM Interaction Logic (Remains here as it uses many main.py components) ---


async def _generate_llm_response_for_chat(
    db_context: DatabaseContext, # Added db_context
    processing_service: ProcessingService,
    application: Application,
    chat_id: int,
    trigger_content_parts: List[Dict[str, Any]],
    user_name: str,
) -> Tuple[Optional[str], Optional[List[Dict[str, Any]]]]:
    """
    Prepares context, message history, calls the ProcessingService, and returns the response.

    Args:
        db_context: The database context to use for storage operations.
        processing_service: The processing service instance.
        application: The Telegram application instance.
        chat_id: The target chat ID.
        trigger_content_parts: List of content parts for the triggering message.
        user_name: The user name to format into the system prompt.

    Returns:
        A tuple: (LLM response string or None, List of tool call info dicts or None).
    """
    logger.debug(
        f"Generating LLM response for chat {chat_id}, triggered by: {trigger_content_parts[0].get('type', 'unknown')}"
    )

    # --- History and Context Preparation ---
    messages: List[Dict[str, Any]] = []
    try:
        history_messages = await storage.get_recent_history( # Use storage directly with context
            db_context=db_context, # Pass context
            chat_id=chat_id,
            limit=MAX_HISTORY_MESSAGES,
            max_age=timedelta(hours=HISTORY_MAX_AGE_HOURS),
        )
    except Exception as hist_err:
        logger.error(f"Failed to get message history for chat {chat_id}: {hist_err}", exc_info=True)
        history_messages = [] # Continue with empty history on error


    # Process history messages, formatting assistant tool calls correctly
    for msg in history_messages:
        if msg["role"] == "assistant" and "tool_calls_info_raw" in msg:
            # Construct the 'tool_calls' structure expected by LiteLLM/OpenAI
            # from the stored 'tool_calls_info_raw'
            reformatted_tool_calls = []
            raw_info_list = msg.get("tool_calls_info_raw", [])
            if isinstance(raw_info_list, list):  # Ensure it's a list
                for raw_call in raw_info_list:
                    # Assuming raw_call is a dict like:
                    # {'call_id': '...', 'function_name': '...', 'arguments': {...}, 'response_content': '...'}
                    if isinstance(raw_call, dict):
                        reformatted_tool_calls.append(
                            {
                                "id": raw_call.get(
                                    "call_id", f"call_{uuid.uuid4()}"
                                ),  # Generate ID if missing
                                "type": "function",
                                "function": {
                                    "name": raw_call.get(
                                        "function_name", "unknown_tool"
                                    ),
                                    # Arguments should already be a JSON string or dict from storage
                                    "arguments": (
                                        json.dumps(raw_call.get("arguments", {}))
                                        if isinstance(raw_call.get("arguments"), dict)
                                        else raw_call.get("arguments", "{}")
                                    ),
                                },
                            }
                        )
                        # Also need to append the corresponding tool response message
                        # The history mechanism currently stores this *within* the assistant message's info block.
                        # We need separate 'assistant' (with tool_calls) and 'tool' messages.
                        # Let's adjust history storage/retrieval or formatting here.

                        # OPTION 1 (Simpler formatting here): Append tool response immediately after
                        messages.append(
                            {
                                "role": "assistant",
                                "content": msg.get(
                                    "content"
                                ),  # Include original content if any
                                "tool_calls": reformatted_tool_calls,
                            }
                        )
                        # Now append the corresponding tool result message for each call
                        for raw_call in raw_info_list:
                            if isinstance(raw_call, dict):
                                messages.append(
                                    {
                                        "role": "tool",
                                        "tool_call_id": raw_call.get(
                                            "call_id", "missing_id"
                                        ),
                                        "content": raw_call.get(
                                            "response_content",
                                            "Error: Missing tool response content",
                                        ),
                                    }
                                )
                    else:
                        logger.warning(
                            f"Skipping non-dict item in raw_tool_calls_info: {raw_call}"
                        )
            else:
                logger.warning(
                    f"Expected list for raw_tool_calls_info, got {type(raw_info_list)}. Skipping tool call reconstruction."
                )
            # Skip adding the original combined message dictionary 'msg' as we added parts separately
        else:
            # Add regular user or assistant messages (without tool calls)
            messages.append({"role": msg["role"], "content": msg["content"]})

    # messages.extend(history_messages) # Removed this line, processing is now done above
    logger.debug(
        f"Processed {len(history_messages)} DB history messages into LLM format."
    )

    # --- Prepare System Prompt Context ---
    system_prompt_template = PROMPTS.get(
        "system_prompt", "You are a helpful assistant. Current time is {current_time}."
    )
    try:
        local_tz = pytz.timezone(TIMEZONE_STR)
        current_local_time = datetime.now(local_tz)
        current_time_str = current_local_time.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception as tz_err:
        logger.error(
            f"Error applying timezone {TIMEZONE_STR}: {tz_err}. Defaulting time format."
        )
        current_time_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    calendar_context_str = ""
    if CALENDAR_CONFIG:
        try:
            upcoming_events = await calendar_integration.fetch_upcoming_events(
                CALENDAR_CONFIG
            )
            today_events_str, future_events_str = (
                calendar_integration.format_events_for_prompt(upcoming_events, PROMPTS)
            )
            calendar_header_template = PROMPTS.get(
                "calendar_context_header",
                "{today_tomorrow_events}\n{next_two_weeks_events}",
            )
            calendar_context_str = calendar_header_template.format(
                today_tomorrow_events=today_events_str,
                next_two_weeks_events=future_events_str,
            ).strip()
        except Exception as cal_err:
            logger.error(
                f"Failed to fetch or format calendar events: {cal_err}", exc_info=True
            )
            calendar_context_str = f"Error retrieving calendar events: {str(cal_err)}"
    else:
        calendar_context_str = "Calendar integration not configured."

    notes_context_str = ""
    try:
        all_notes = await storage.get_all_notes(db_context=db_context) # Use storage directly with context
        if all_notes:
            notes_list_str = ""
            note_item_format = PROMPTS.get("note_item_format", "- {title}: {content}")
            for note in all_notes:
                notes_list_str += (
                    note_item_format.format(
                        title=note["title"], content=note["content"]
                    )
                    + "\n"
                )
            notes_context_header_template = PROMPTS.get(
                "notes_context_header", "Relevant notes:\n{notes_list}"
            )
            notes_context_str = notes_context_header_template.format(
                notes_list=notes_list_str.strip()
            )
        else:
            notes_context_str = PROMPTS.get("no_notes", "No notes available.")
    except Exception as note_err:
        logger.error(f"Failed to get notes for context: {note_err}", exc_info=True)
        notes_context_str = "Error retrieving notes."

    final_system_prompt = system_prompt_template.format(
        user_name=user_name,
        current_time=current_time_str,
        calendar_context=calendar_context_str,
        notes_context=notes_context_str,
    ).strip()

    if final_system_prompt:
        messages.insert(0, {"role": "system", "content": final_system_prompt})
        logger.debug("Prepended system prompt to LLM messages.")
    else:
        logger.warning("Generated empty system prompt.")

    # --- Add the triggering message content ---
    # Note: For callbacks, the role might ideally be 'system' or a specific 'callback' role,
    # but using 'user' is often necessary for the LLM to properly attend to it as the primary input.
    # If LLM behavior is odd, consider experimenting with the role here.
    trigger_message = {
        "role": "user",  # Treat trigger as user input for processing flow
        "content": trigger_content_parts,
    }
    messages.append(trigger_message)
    logger.debug(f"Appended trigger message to LLM history: {trigger_message}")

    # --- Call Processing Service ---
    # Tool definitions are now fetched inside process_message
    try:
        # Call the process_message method, passing db_context and application instance
        llm_response_content, tool_info = await processing_service.process_message(
            db_context=db_context, # Pass context
            messages=messages,
            chat_id=chat_id,
            application=application,
        )
        return llm_response_content, tool_info
    except Exception as e:
        logger.error(
            f"Error during ProcessingService interaction for chat {chat_id}: {e}",
            exc_info=True,
        )
        # Return None for both parts on error
        return None, None


# --- Telegram Bot Handlers (Moved to TelegramBotHandler) ---
# start, process_chat_queue, message_handler, error_handler moved


# --- Signal Handlers ---
async def shutdown_handler(signal_name: str):
    """Initiates graceful shutdown."""
    logger.warning(f"Received signal {signal_name}. Initiating shutdown...")
    shutdown_event.set()
    # Ensure the event is set to signal other parts of the application
    if not shutdown_event.is_set():
        shutdown_event.set()

    # --- Graceful Task Cancellation ---
    # Get the current loop *before* it might be stopped
    loop = asyncio.get_running_loop()
    tasks = [t for t in asyncio.all_tasks(loop=loop) if t is not asyncio.current_task()]
    if tasks:
        logger.info(f"Cancelling {len(tasks)} outstanding tasks...")
        for task in tasks:
            task.cancel()
        # Wait for tasks to finish cancelling
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Outstanding tasks cancelled.")
    else:
        logger.info("No outstanding tasks to cancel.")

    # --- Stop Services (Order might matter) ---
    if application and application.updater:
        logger.info("Stopping Telegram polling...")
        await application.updater.stop()
        logger.info("Telegram polling stopped.")

    # Uvicorn server shutdown is handled in main_async when shutdown_event is set

    # Close MCP sessions via the exit stack
    logger.info("Closing MCP server connections...")
    await mcp_exit_stack.aclose()
    logger.info("MCP server connections closed.")

    # Stop the application itself
    if application:
        logger.info("Shutting down Telegram application...")
        await application.shutdown()
        logger.info("Telegram application shut down.")


def reload_config_handler(signum, frame):
    """Handles SIGHUP for config reloading (placeholder)."""
    logger.info("Received SIGHUP signal. Reloading configuration...")
    load_config()
    # Potentially restart parts of the application if needed,
    # but be careful with state. For now, just log and reload vars.


# --- Main Application Setup & Run ---
async def main_async(cli_args: argparse.Namespace) -> None:  # Accept parsed args
    """Initializes and runs the bot application."""
    global application
    logger.info(f"Using model: {cli_args.model}")  # Use cli_args

    # --- Validate Essential Config from args ---
    if not cli_args.telegram_token:
        raise ValueError(
            "Telegram Bot Token must be provided via --telegram-token or TELEGRAM_BOT_TOKEN env var"
        )
    if not cli_args.openrouter_api_key:
        raise ValueError(
            "OpenRouter API Key must be provided via --openrouter-api-key or OPENROUTER_API_KEY env var"
        )

    # Set OpenRouter API key for LiteLLM (if using LiteLLMClient)
    os.environ["OPENROUTER_API_KEY"] = cli_args.openrouter_api_key

    # --- LLM Client and Processing Service Instantiation ---
    # TODO: Add logic to choose LLM client based on config (e.g., args, env vars)
    # For now, default to LiteLLMClient
    llm_client: LLMInterface = LiteLLMClient(model=cli_args.model)
    # Example for recording:
    # live_client = LiteLLMClient(model=cli_args.model)
    # llm_client = RecordingLLMClient(wrapped_client=live_client, recording_path="llm_interactions.jsonl")
    # Example for playback:
    # llm_client = PlaybackLLMClient(recording_path="llm_interactions.jsonl")

    # Initialize database schema first (needed by ProcessingService potentially via tools)
    await init_db()
    # Load MCP config and connect to servers
    await load_mcp_config_and_connect()  # Populates mcp_sessions, mcp_tools, tool_name_to_server_id

    # --- Instantiate Tool Providers ---
    local_provider = LocalToolsProvider(
        definitions=local_tools_definition, implementations=local_tool_implementations
    )
    mcp_provider = MCPToolsProvider(
        mcp_definitions=mcp_tools,  # Use discovered MCP tool definitions
        mcp_sessions=mcp_sessions,
        tool_name_to_server_id=tool_name_to_server_id,
    )
    # Combine providers
    try:
        composite_provider = CompositeToolsProvider(
            providers=[local_provider, mcp_provider]
        )
        # Eagerly fetch definitions once to cache and validate
        await composite_provider.get_tool_definitions()
        logger.info("CompositeToolsProvider initialized and validated.")
    except ValueError as provider_err:
        logger.critical(
            f"Failed to initialize CompositeToolsProvider: {provider_err}",
            exc_info=True,
        )
        # Exit or handle error appropriately - critical failure
        raise SystemExit(f"Tool provider initialization failed: {provider_err}")

    # --- Instantiate Processing Service ---
    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,  # Inject the composite provider
    )
    logger.info(
        f"ProcessingService initialized with {type(llm_client).__name__} and {type(composite_provider).__name__}."
    )

    # --- Telegram Application Setup ---
    application = ApplicationBuilder().token(cli_args.telegram_token).build()

    # Store the ProcessingService instance in bot_data for access in handlers
    application.bot_data["processing_service"] = processing_service
    logger.info("Stored ProcessingService instance in application.bot_data.")

    # --- Instantiate and Register Telegram Bot Handler ---
    telegram_bot_handler = TelegramBotHandler(
        application=application,
        allowed_chat_ids=ALLOWED_CHAT_IDS,
        developer_chat_id=DEVELOPER_CHAT_ID,
        generate_llm_response_func=_generate_llm_response_for_chat, # Pass the helper function
        get_db_context_func=get_db_context, # Pass the context getter
    )
    telegram_bot_handler.register_handlers() # Register handlers from the class

    # Initialize application (loads persistence, etc.)
    await application.initialize()

    # Start polling
    await application.start()
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("Bot polling started.")  # Updated log message

    # --- Uvicorn Server Setup ---
    config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)

    # Run Uvicorn server concurrently (polling is already running)
    # telegram_task = asyncio.create_task(application.updater.start_polling(allowed_updates=Update.ALL_TYPES)) # Removed duplicate start_polling
    web_server_task = asyncio.create_task(server.serve())
    logger.info("Web server running on http://0.0.0.0:8000")

    # Pass the ProcessingService instance to the task worker module
    # The task worker will use this service, which internally uses the tools provider
    task_worker.set_processing_service(processing_service)
    # No longer need to pass MCP state separately to task worker
    # task_worker.set_mcp_state(mcp_sessions, mcp_tools, tool_name_to_server_id) # Removed

    # Start the task queue worker, passing the notification event
    worker_id = (
        f"worker-{uuid.uuid4()}"  # Generate a unique ID for this worker instance
    )
    task_worker_task = asyncio.create_task(
        task_worker.task_worker_loop(worker_id, new_task_event)
    )

    # Wait until shutdown signal is received
    await shutdown_event.wait()

    logger.info("Shutdown signal received. Stopping services...")

    # Stop polling first
    await application.updater.stop()
    logger.info("Telegram polling stopped.")

    # Signal Uvicorn to shut down gracefully
    server.should_exit = True
    # Wait for Uvicorn to finish
    await web_server_task
    logger.info("Web server stopped.")

    # Polling task cancellation is handled by application.updater.stop() and application.shutdown()
    # Task worker cancellation is handled by the main shutdown_handler.
    # No need to manually cancel telegram_task or task_worker anymore.

    logger.info("All services stopped. Final shutdown.")
    # Application shutdown is handled by the signal handler which calls shutdown_handler


def main() -> int:  # Return an exit code
    """Sets up argument parsing, event loop, and signal handlers."""
    # --- Argument Parsing (Defined and Executed Here) ---
    parser = argparse.ArgumentParser(description="Family Assistant Bot")
    parser.add_argument(
        "--telegram-token",
        default=os.getenv("TELEGRAM_BOT_TOKEN"),
        help="Telegram Bot Token (overrides .env)",
    )
    parser.add_argument(
        "--openrouter-api-key",
        default=os.getenv("OPENROUTER_API_KEY"),
        help="OpenRouter API Key (overrides .env)",
    )
    parser.add_argument(
        "--model",
        # Default model updated based on previous usage and module-level definition
        default=os.getenv("LLM_MODEL", "openrouter/google/gemini-2.5-flash-preview"),
        help="LLM model to use (e.g., openrouter/google/gemini-flash-2.5-flash-preview)",
    )
    args = parser.parse_args()  # Parse args here

    # --- Event Loop and Signal Handlers ---
    loop = asyncio.get_event_loop()

    signal_map = {
        signal.SIGINT: "SIGINT",
        signal.SIGTERM: "SIGTERM",
    }
    for sig_num, sig_name in signal_map.items():
        # Use a default argument in the lambda that captures the current sig_name
        loop.add_signal_handler(
            sig_num, lambda name=sig_name: asyncio.create_task(shutdown_handler(name))
        )

    # SIGHUP for config reload (only on Unix-like systems)
    if hasattr(signal, "SIGHUP"):
        try:
            loop.add_signal_handler(
                signal.SIGHUP, reload_config_handler, signal.SIGHUP, None
            )
        except NotImplementedError:
            logger.warning("SIGHUP signal handler not supported on this platform.")

    try:
        logger.info("Starting application...")
        # Pass parsed args to main_async
        loop.run_until_complete(main_async(args))
    except ValueError as config_err:  # Catch config validation errors
        logger.critical(f"Configuration error: {config_err}")
        return 1  # Return non-zero exit code
    except (KeyboardInterrupt, SystemExit) as ex:
        logger.warning(f"Received {type(ex).__name__}, initiating shutdown.")
        # Ensure shutdown runs if loop was interrupted directly
        if not shutdown_event.is_set():
            # Run the async shutdown handler within the loop
            loop.run_until_complete(shutdown_handler(type(ex).__name__))
    finally:
        # Task cleanup is now handled within shutdown_handler
        logger.info("Closing event loop.")
        loop.close()
        logger.info("Application finished.")
    return 0  # Return 0 on successful shutdown


if __name__ == "__main__":
    sys.exit(main())  # Exit with the return code from main()
