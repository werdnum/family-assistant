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
import traceback  # Keep for error handler if needed elsewhere, or remove if only used in bot's handler
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

# from telegram import Update # No longer needed here
# from telegram.constants import ChatAction, ParseMode # Moved to telegram_bot.py
# from telegram.ext import ( # No longer needed here
#     Application,
#     ApplicationBuilder,
#     # CallbackContext, # Moved to telegram_bot.py
#     # CommandHandler, # Moved to telegram_bot.py
#     # ContextTypes, # Moved to telegram_bot.py
#     # MessageHandler, # Moved to telegram_bot.py
#     # filters, # Moved to telegram_bot.py
# )
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
from family_assistant.storage.context import (
    DatabaseContext,
    get_db_context,
)  # Import DatabaseContext and getter

# Import calendar functions
from family_assistant import calendar_integration

# Import the Telegram service class
from .telegram_bot import TelegramService  # Updated import

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
# application: Optional[Application] = None # Removed global application instance
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


# --- Core LLM Interaction Logic (Moved to ProcessingService) ---
# _generate_llm_response_for_chat moved


# --- Telegram Bot Handlers (Moved to TelegramBotHandler) ---
# start, process_chat_queue, message_handler, error_handler moved


# --- Signal Handlers ---
async def shutdown_handler(
    signal_name: str, telegram_service: Optional[TelegramService]
):  # Accept service instance
    """Initiates graceful shutdown."""
    logger.warning(f"Received signal {signal_name}. Initiating shutdown...")
    # Ensure the event is set to signal other parts of the application
    if not shutdown_event.is_set():  # Check before setting
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
    # Stop Telegram polling via the service
    # Need access to the telegram_service instance created in main_async
    # Option 1: Make telegram_service global (less ideal)
    # Stop Telegram polling via the passed service instance
    if telegram_service:
        await telegram_service.stop_polling()
    else:
        logger.warning("TelegramService instance was None during shutdown.")

    # Uvicorn server shutdown is handled in main_async when shutdown_event is set

    # Close MCP sessions via the exit stack
    logger.info("Closing MCP server connections...")
    await mcp_exit_stack.aclose()
    logger.info("MCP server connections closed.")

    # Telegram application shutdown is now handled within telegram_service.stop_polling()


def reload_config_handler(signum, frame):
    """Handles SIGHUP for config reloading (placeholder)."""
    logger.info("Received SIGHUP signal. Reloading configuration...")
    load_config()
    # Potentially restart parts of the application if needed,
    # but be careful with state. For now, just log and reload vars.


# --- Main Application Setup & Run ---
async def main_async(
    cli_args: argparse.Namespace,
) -> Optional[TelegramService]:  # Return service instance or None
    """Initializes and runs the bot application."""
    # global application # Removed
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
        # Pass configuration needed for context building
        prompts=PROMPTS,
        calendar_config=CALENDAR_CONFIG,
        timezone_str=TIMEZONE_STR,
        max_history_messages=MAX_HISTORY_MESSAGES,
        history_max_age_hours=HISTORY_MAX_AGE_HOURS,
    )
    logger.info(
        f"ProcessingService initialized with {type(llm_client).__name__}, {type(composite_provider).__name__} and configuration."
    )

    # --- Instantiate Telegram Service ---
    telegram_service = TelegramService(
        telegram_token=cli_args.telegram_token,
        allowed_chat_ids=ALLOWED_CHAT_IDS,
        developer_chat_id=DEVELOPER_CHAT_ID,
        processing_service=processing_service,
        get_db_context_func=get_db_context,
    )

    # Start polling using the service method
    await telegram_service.start_polling()

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

    # Stop polling using the service method (already called by shutdown_handler if setup correctly)
    # await telegram_service.stop_polling() # This is now handled by the signal handler

    # Signal Uvicorn to shut down gracefully
    server.should_exit = True
    # Wait for Uvicorn to finish
    await web_server_task
    logger.info("Web server stopped.")

    # Polling task cancellation is handled by application.updater.stop() and application.shutdown()
    # Task worker cancellation is handled by the main shutdown_handler.
    # No need to manually cancel task_worker anymore (handled by shutdown_handler).

    logger.info("All services stopped. Final shutdown.")
    # Telegram application shutdown is handled by telegram_service.stop_polling() called from shutdown_handler

    return telegram_service  # Return the created service instance


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

    # --- Event Loop and Signal Handlers ---
    loop = asyncio.get_event_loop()

    # Placeholder for telegram_service instance to be passed to handler
    # This is slightly tricky as the service is created *inside* main_async
    # We might need to create the service earlier or use a different mechanism.
    # Let's adjust main_async slightly to create the service earlier.

    # --- Run main_async ---
    telegram_service_instance = None  # Initialize
    try:
        logger.info("Starting application...")
        # Pass parsed args to main_async
        # main_async will now return the created telegram_service instance
        telegram_service_instance = loop.run_until_complete(main_async(args))

        # --- Setup Signal Handlers *after* service creation ---
        if telegram_service_instance:
            signal_map = {
                signal.SIGINT: "SIGINT",
                signal.SIGTERM: "SIGTERM",
            }
            for sig_num, sig_name in signal_map.items():
                # Pass the service instance to the shutdown handler lambda
                loop.add_signal_handler(
                    sig_num,
                    lambda name=sig_name, service=telegram_service_instance: asyncio.create_task(
                        shutdown_handler(name, service)  # Pass service instance
                    ),
                )
        else:
            logger.error(
                "Failed to create TelegramService, signal handlers not fully set up."
            )
            # Fallback simple handler if service creation failed
            for sig_num, sig_name in signal_map.items():
                loop.add_signal_handler(
                    sig_num,
                    lambda name=sig_name: asyncio.create_task(
                        shutdown_handler(name, None)
                    ),
                )

        # --- Setup SIGHUP Handler (Inside the main try block, after other signals) ---
        if hasattr(signal, "SIGHUP"):
            try:
                loop.add_signal_handler(
                    signal.SIGHUP, reload_config_handler, signal.SIGHUP, None
                )
                logger.info("SIGHUP handler registered for config reload.")
            except NotImplementedError:
                logger.warning("SIGHUP signal handler not supported on this platform.")

        # The main loop implicitly runs after this point, waiting for signals or KeyboardInterrupt within the outer try block.

    # The except and finally blocks corresponding to the *outer* try block remain below.
    except ValueError as config_err:  # Catch config validation errors from main_async
        logger.critical(f"Configuration error during startup: {config_err}")
        return 1  # Return non-zero exit code
    except (KeyboardInterrupt, SystemExit) as ex:
        logger.warning(f"Received {type(ex).__name__}, initiating shutdown.")
        # Ensure shutdown runs if loop was interrupted directly
        if not shutdown_event.is_set():
            # Run the async shutdown handler within the loop, passing the service instance
            loop.run_until_complete(
                shutdown_handler(type(ex).__name__, telegram_service_instance)
            )
    finally:
        # Task cleanup is handled within shutdown_handler
        logger.info("Closing event loop.")
        # Ensure loop is closed only if it's running
        # Removing loop.close() as it can cause issues if called incorrectly.
        # if loop.is_running():
        #      loop.close()
        #      logger.info("Event loop closed.")
        # else:
        #      logger.info("Event loop was already closed.")

        logger.info("Application finished.")

    return 0  # Return 0 on successful shutdown


if __name__ == "__main__":
    sys.exit(main())  # Exit with the return code from main()
