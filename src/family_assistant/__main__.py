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

import zoneinfo
from dotenv import load_dotenv
import uvicorn
import functools # Import functools

# Import task worker CLASS, handlers, and events
from family_assistant.task_worker import (
    TaskWorker,
    handle_log_message,
    handle_llm_callback,
    handle_index_email, # Keep email handler import for now
    shutdown_event, # Import event directly
    new_task_event, # Import event directly
)

# Import the ProcessingService and LLM interface/clients
from family_assistant.processing import ProcessingService
from family_assistant.llm import (
    LLMInterface,
    LiteLLMClient,
    RecordingLLMClient,
    PlaybackLLMClient,
)

# Import Embedding interface/clients
import family_assistant.embeddings as embeddings
from family_assistant.embeddings import (
    EmbeddingGenerator,
    LiteLLMEmbeddingGenerator,
    SentenceTransformerEmbeddingGenerator, # If available
    MockEmbeddingGenerator, # For testing
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

# Import indexing components
from family_assistant.indexing.email_indexer import handle_index_email, set_indexing_dependencies
from family_assistant.indexing.document_indexer import DocumentIndexer # Import the class

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
CONFIG_FILE_PATH = "config.yaml" # Path to the new config file

# Events are now imported directly
# shutdown_event = task_worker.shutdown_event # Removed
# new_task_event = task_worker.new_task_event # Removed

# --- Global Variables ---
# Configuration will be loaded into a dictionary instead of individual globals
# ALLOWED_CHAT_IDS: list[int] = [] # Replaced by config dict
# DEVELOPER_CHAT_ID: Optional[int] = None # Replaced by config dict
# PROMPTS: Dict[str, str] = {} # Replaced by config dict
# CALENDAR_CONFIG: Dict[str, Any] = {} # Replaced by config dict
# TIMEZONE_STR: str = "UTC" # Replaced by config dict
# APP_CONFIG: Dict[str, Any] = {} # Replaced by config dict

# State variables remain global for now
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


# Task handler registry is now part of the TaskWorker instance


# Use task_worker's helper function (remains module-level)
# format_llm_response_for_telegram = task_worker.format_llm_response_for_telegram
# ^^^ This is actually defined in task_worker.py but not used here, remove if not needed


# Task worker loop is now the run() method of the TaskWorker class


# --- Configuration Loading ---
def load_config(config_file_path: str = CONFIG_FILE_PATH) -> Dict[str, Any]:
    """
    Loads configuration according to the defined hierarchy:
    Defaults -> config.yaml -> Environment Variables.
    CLI arguments are applied *after* this function runs.

    Args:
        config_file_path: Path to the main YAML configuration file.

    Returns:
        A dictionary containing the resolved configuration.
    """
    # 1. Code Defaults
    config_data: Dict[str, Any] = {
        "telegram_token": None,
        "openrouter_api_key": None,
        "allowed_chat_ids": [],
        "developer_chat_id": None,
        "model": "openrouter/google/gemini-2.5-pro-preview-03-25",  # Default model
        "embedding_model": "gemini/gemini-embedding-exp-03-07",  # Default embedding model
        "embedding_dimensions": 1536,  # Default dimension
        "timezone": "UTC",
        "database_url": "sqlite+aiosqlite:///family_assistant.db",  # Default DB
        "max_history_messages": 5,
        "history_max_age_hours": 24,
        "litellm_debug": False,
        "calendar_config": {},
        "llm_parameters": {},
        "prompts": {},
        "mcp_config": {"mcpServers": {}},  # Default empty MCP config
    }
    logger.info("Initialized config with code defaults.")

    # 2. Load config.yaml
    try:
        with open(config_file_path, "r", encoding="utf-8") as f:
            yaml_config = yaml.safe_load(f)
            if isinstance(yaml_config, dict):
                # Merge YAML config, overwriting defaults
                config_data.update(yaml_config)
                logger.info(f"Loaded and merged configuration from {config_file_path}")
            else:
                logger.warning(f"Config file {config_file_path} is not a valid dictionary. Ignoring.")
    except FileNotFoundError:
        logger.warning(f"{config_file_path} not found. Using default configurations.")
    except yaml.YAMLError as e:
        logger.error(f"Error parsing {config_file_path}: {e}. Using previous defaults.")

    # 3. Load Environment Variables (overriding config file)
    load_dotenv() # Load .env file if present

    # Secrets (should ONLY come from env)
    config_data["telegram_token"] = os.getenv("TELEGRAM_BOT_TOKEN", config_data["telegram_token"])
    config_data["openrouter_api_key"] = os.getenv("OPENROUTER_API_KEY", config_data["openrouter_api_key"])
    config_data["database_url"] = os.getenv("DATABASE_URL", config_data["database_url"])
    caldav_pass_env = os.getenv("CALDAV_PASSWORD") # Load separately

    # Other Env Vars
    config_data["model"] = os.getenv("LLM_MODEL", config_data["model"])
    config_data["embedding_model"] = os.getenv("EMBEDDING_MODEL", config_data["embedding_model"])
    config_data["embedding_dimensions"] = int(os.getenv("EMBEDDING_DIMENSIONS", str(config_data["embedding_dimensions"])))
    config_data["timezone"] = os.getenv("TIMEZONE", config_data["timezone"])
    config_data["litellm_debug"] = os.getenv("LITELLM_DEBUG", str(config_data["litellm_debug"])).lower() in ("true", "1", "yes")

    # Parse comma-separated lists from Env Vars
    allowed_ids_str = os.getenv("ALLOWED_CHAT_IDS")
    if allowed_ids_str is not None: # Only override if env var is explicitly set
        try:
            config_data["allowed_chat_ids"] = [
                int(cid.strip()) for cid in allowed_ids_str.split(",") if cid.strip()
            ]
        except ValueError:
            logger.error("Invalid format for ALLOWED_CHAT_IDS env var. Using previous value.")

    dev_id_str = os.getenv("DEVELOPER_CHAT_ID")
    if dev_id_str is not None: # Only override if env var is explicitly set
        try:
            config_data["developer_chat_id"] = int(dev_id_str)
        except ValueError:
            logger.error("Invalid DEVELOPER_CHAT_ID env var. Using previous value.")

    # Calendar Config from Env Vars (overrides anything in config.yaml for calendars)
    caldav_user_env = os.getenv("CALDAV_USERNAME")
    caldav_urls_str_env = os.getenv("CALDAV_CALENDAR_URLS")
    ical_urls_str_env = os.getenv("ICAL_URLS")

    temp_calendar_config = {}
    if caldav_user_env and caldav_pass_env and caldav_urls_str_env:
        caldav_urls_env = [url.strip() for url in caldav_urls_str_env.split(",") if url.strip()]
        if caldav_urls_env:
            temp_calendar_config["caldav"] = {
                "username": caldav_user_env,
                "password": caldav_pass_env, # Use password loaded earlier
                "calendar_urls": caldav_urls_env,
            }
            logger.info("Loaded CalDAV config from environment variables.")
    if ical_urls_str_env:
        ical_urls_env = [url.strip() for url in ical_urls_str_env.split(",") if url.strip()]
        if ical_urls_env:
            temp_calendar_config["ical"] = {"urls": ical_urls_env}
            logger.info("Loaded iCal config from environment variables.")

    # Only update calendar_config if env vars provided valid config
    if temp_calendar_config:
        config_data["calendar_config"] = temp_calendar_config
    elif not config_data.get("calendar_config"): # If no config from yaml either
        logger.warning("No calendar sources configured in config file or environment variables.")

    # Validate Timezone
    try:
        zoneinfo.ZoneInfo(config_data["timezone"])
    except zoneinfo.ZoneInfoNotFoundError:
        logger.error(f"Invalid timezone '{config_data['timezone']}'. Defaulting to UTC.")
        config_data["timezone"] = "UTC"

    # 4. Load other config files (Prompts, MCP)
    # Load prompts from YAML file
    try:
        with open("prompts.yaml", "r", encoding="utf-8") as f:
            loaded_prompts = yaml.safe_load(f)
            if isinstance(loaded_prompts, dict):
                config_data["prompts"] = loaded_prompts # Store in config dict
                logger.info("Successfully loaded prompts from prompts.yaml")
            else:
                logger.error("Failed to load prompts: prompts.yaml is not a valid dictionary.")
    except FileNotFoundError:
        logger.error("prompts.yaml not found. Using default prompt structures.")
    except yaml.YAMLError as e:
        logger.error(f"Error parsing prompts.yaml: {e}")

    # Load MCP config from JSON file
    mcp_config_path = "mcp_config.json"
    try:
        with open(mcp_config_path, "r", encoding="utf-8") as f:
            loaded_mcp_config = json.load(f)
            if isinstance(loaded_mcp_config, dict):
                config_data["mcp_config"] = loaded_mcp_config # Store in config dict
                logger.info(f"Successfully loaded MCP config from {mcp_config_path}")
            else:
                logger.error(f"Failed to load MCP config: {mcp_config_path} is not a valid dictionary.")
    except FileNotFoundError:
        logger.info(f"{mcp_config_path} not found. MCP features may be disabled.")
    except json.JSONDecodeError as e:
        logger.error(f"Error decoding {mcp_config_path}: {e}")

    # Log final loaded non-secret config for verification
    loggable_config = {k: v for k, v in config_data.items() if k not in ['telegram_token', 'openrouter_api_key', 'database_url']}
    if 'calendar_config' in loggable_config and 'caldav' in loggable_config['calendar_config']:
        loggable_config['calendar_config']['caldav'].pop('password', None) # Remove password before logging
    logger.info(f"Final configuration loaded (excluding secrets): {json.dumps(loggable_config, indent=2, default=str)}")

    return config_data


# --- MCP Configuration Loading & Connection ---
async def load_mcp_config_and_connect(mcp_config: Dict[str, Any]): # Accept MCP config dict
    """Connects to MCP servers defined in the config and discovers tools."""
    global mcp_sessions, mcp_tools, tool_name_to_server_id, mcp_exit_stack # Keep using globals for state

    # Clear previous state if reloading? For now, assume it runs once at start.
    mcp_sessions.clear()
    mcp_tools.clear()
    tool_name_to_server_id.clear()
    # Note: We don't recreate mcp_exit_stack here, it's managed globally.

    mcp_server_configs = mcp_config.get("mcpServers", {}) # Get servers from passed config
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
# Define parser here, but parse arguments later in main() after loading config
parser = argparse.ArgumentParser(description="Family Assistant Bot")
parser.add_argument(
    "--telegram-token",
    default=None, # Default is None, will be loaded from env/config
    help="Telegram Bot Token (overrides environment variable)",
)
parser.add_argument(
    "--openrouter-api-key",
    default=None, # Default is None, will be loaded from env/config
    help="OpenRouter API Key (overrides environment variable)",
)
parser.add_argument(
    "--model",
    default=None, # Default is None, will be loaded from env/config
    help="LLM model identifier (overrides config file and environment variable)",
)
parser.add_argument(
    "--embedding-model",
    default=None, # Default is None, will be loaded from env/config
    help="Embedding model identifier (overrides config file and environment variable)",
)
parser.add_argument(
    '--embedding-dimensions',
    type=int,
    default=None, # Default is None, will be loaded from env/config
    help="Embedding model dimensionality (overrides config file and environment variable)"
)
# Add argument for config file path?
# parser.add_argument('--config', default='config.yaml', help='Path to the main YAML configuration file.')

# --- Signal Handlers ---
# Define handlers before main() where they are registered
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
    config: Dict[str, Any] # Accept the resolved config dictionary
) -> Optional[TelegramService]:  # Return service instance or None
    """Initializes and runs the bot application using the provided configuration."""
    # global application # Removed
    logger.info(f"Using model: {config['model']}")

    # --- Validate Essential Config ---
    # Secrets should have been loaded by load_config() from env vars
    if not config.get("telegram_token"):
        raise ValueError("Telegram Bot Token is missing (check env var TELEGRAM_BOT_TOKEN).")
    if not config.get("openrouter_api_key"):
        raise ValueError("OpenRouter API Key is missing (check env var OPENROUTER_API_KEY).")

    # Set OpenRouter API key for LiteLLM (if using LiteLLMClient)
    # This might be redundant if LiteLLM picks it up automatically, but explicit is okay.
    os.environ["OPENROUTER_API_KEY"] = config["openrouter_api_key"]

    # --- LLM Client Instantiation ---
    llm_client: LLMInterface = LiteLLMClient(
        model=config["model"],
        model_parameters=config.get("llm_parameters", {}) # Get from config dict
    )

    # --- Embedding Generator Instantiation ---
    embedding_generator: EmbeddingGenerator
    embedding_model_name = config["embedding_model"]
    embedding_dimensions = config["embedding_dimensions"]
    # Example check for local model (adjust condition as needed)
    if embedding_model_name.startswith("/") or embedding_model_name in ["all-MiniLM-L6-v2", "other-local-model-name"]:
         try:
             if "SentenceTransformerEmbeddingGenerator" not in dir(embeddings):
                 raise ImportError("sentence-transformers library not installed, cannot use local embedding model.")
             embedding_generator = embeddings.SentenceTransformerEmbeddingGenerator(
                 model_name_or_path=embedding_model_name
                 # Pass dimensions if the local model class supports it? Check SentenceTransformer docs.
             )
         except (ImportError, ValueError, RuntimeError) as local_embed_err:
             logger.critical(f"Failed to initialize local embedding model '{embedding_model_name}': {local_embed_err}")
             raise SystemExit(f"Local embedding model initialization failed: {local_embed_err}")
    else:
        # Assume API-based model via LiteLLM
        embedding_generator = LiteLLMEmbeddingGenerator(
            model=embedding_model_name,
            dimensions=embedding_dimensions # Pass dimensions from config
        )

    logger.info(f"Using embedding generator: {type(embedding_generator).__name__} with model: {embedding_generator.model_name}")

    # --- Store generator in app state for web server access ---
    fastapi_app.state.embedding_generator = embedding_generator
    logger.info("Stored embedding generator in FastAPI app state.")

    # Initialize database schema first
    # init_db uses the engine configured in storage/base.py, which reads DATABASE_URL env var.
    # No need to pass database_url here.
    await init_db()
    # Initialize vector DB components (extension, indexes)
    # get_db_context uses the engine configured in storage/base.py by default.
    async with await get_db_context() as db_ctx:
        await storage.init_vector_db(db_ctx) # Initialize vector specific parts

    # Load MCP config and connect to servers using config dict
    await load_mcp_config_and_connect(config["mcp_config"]) # Pass MCP config part

    # --- Instantiate Tool Providers ---
    local_provider = LocalToolsProvider(
        definitions=local_tools_definition,
        implementations=local_tool_implementations,
        embedding_generator=embedding_generator,
        calendar_config=config["calendar_config"], # Get from config dict
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
        await composite_provider.get_tool_definitions()
        logger.info("CompositeToolsProvider initialized and validated.")
    except ValueError as provider_err:
        logger.critical(f"Failed to initialize CompositeToolsProvider: {provider_err}", exc_info=True)
        raise SystemExit(f"Tool provider initialization failed: {provider_err}")

    # --- Instantiate Processing Service ---
    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        prompts=config["prompts"], # Get from config dict
        calendar_config=config["calendar_config"], # Get from config dict
        timezone_str=config["timezone"], # Get from config dict
        max_history_messages=config["max_history_messages"], # Get from config dict
        history_max_age_hours=config["history_max_age_hours"], # Get from config dict
    )
    logger.info(f"ProcessingService initialized with configuration.")

    # --- Instantiate Indexers ---
    document_indexer = DocumentIndexer(embedding_generator=embedding_generator)
    set_indexing_dependencies(embedding_generator=embedding_generator, llm_client=llm_client)

    # --- Instantiate Telegram Service ---
    telegram_service = TelegramService(
        telegram_token=config["telegram_token"], # Get from config dict
        allowed_chat_ids=config["allowed_chat_ids"], # Get from config dict
        developer_chat_id=config["developer_chat_id"], # Get from config dict
        processing_service=processing_service,
        get_db_context_func=get_db_context, # Pass the function directly
    )

    # Start polling using the service method
    await telegram_service.start_polling()

    # --- Store service in app state for web server access ---
    fastapi_app.state.telegram_service = telegram_service
    logger.info("Stored TelegramService instance in FastAPI app state.")

    # --- Uvicorn Server Setup ---
    config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)

    # Run Uvicorn server concurrently (polling is already running)
    # telegram_task = asyncio.create_task(application.updater.start_polling(allowed_updates=Update.ALL_TYPES)) # Removed duplicate start_polling
    web_server_task = asyncio.create_task(server.serve())
    logger.info("Web server running on http://0.0.0.0:8000")

    # --- Instantiate Task Worker ---
    task_worker_instance = TaskWorker(
        processing_service=processing_service,
        # Pass other dependencies if needed by its methods or handlers registered here
    )

    # --- Register Task Handlers with the Worker Instance ---
    task_worker_instance.register_task_handler("log_message", handle_log_message)
    # Register document processing handler from the indexer instance
    task_worker_instance.register_task_handler(
        "process_uploaded_document", document_indexer.process_document
    )
    # Register email indexing handler (still using module-level function for now)
    # TODO: Refactor email_indexer and register its method here
    task_worker_instance.register_task_handler("index_email", handle_index_email)
    # Register LLM callback handler, pre-binding the processing_service dependency
    task_worker_instance.register_task_handler(
        "llm_callback",
        functools.partial(handle_llm_callback, processing_service)
    )

    logger.info(f"Registered task handlers for worker {task_worker_instance.worker_id}: {list(task_worker_instance.get_task_handlers().keys())}")

    # Start the task queue worker using the instance's run method
    task_worker_task = asyncio.create_task(
        task_worker_instance.run(new_task_event) # Pass the event to the run method
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
    """Loads config, parses args, sets up event loop, and runs the application."""

    # 1. Load Configuration (Defaults -> YAML -> Env Vars)
    config_data = load_config()

    # 2. Parse CLI Arguments
    args = parser.parse_args()

    # 3. Apply CLI Overrides (CLI > Env > YAML > Defaults)
    if args.telegram_token is not None:
        config_data["telegram_token"] = args.telegram_token
        logger.info("Overriding Telegram token with CLI argument.")
    if args.openrouter_api_key is not None:
        config_data["openrouter_api_key"] = args.openrouter_api_key
        logger.info("Overriding OpenRouter API key with CLI argument.")
    if args.model is not None:
        config_data["model"] = args.model
        logger.info(f"Overriding LLM model with CLI argument: {args.model}")
    if args.embedding_model is not None:
        config_data["embedding_model"] = args.embedding_model
        logger.info(f"Overriding embedding model with CLI argument: {args.embedding_model}")
    if args.embedding_dimensions is not None:
        config_data["embedding_dimensions"] = args.embedding_dimensions
        logger.info(f"Overriding embedding dimensions with CLI argument: {args.embedding_dimensions}")
    # Add overrides for other CLI args if introduced (e.g., --config)

    # --- Event Loop and Signal Handlers ---
    loop = asyncio.get_event_loop()
    telegram_service_instance = None  # Initialize

    try:
        logger.info("Starting application...")
        # Pass the final resolved config dictionary to main_async
        telegram_service_instance = loop.run_until_complete(main_async(config_data))

        # --- Setup Signal Handlers *after* service creation ---
        # Pass the service instance to the shutdown handler lambda
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
