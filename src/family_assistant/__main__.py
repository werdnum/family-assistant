import argparse
import asyncio
import contextlib
import html
import argparse
import copy
import argparse
import asyncio
import contextlib
import html

import json
import logging

import os
import signal
import sys
import traceback
import uuid
import yaml
import mcp
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Tuple

import zoneinfo
from dotenv import load_dotenv
import uvicorn
import functools  # Import functools

# Import task worker CLASS, handlers, and events
from family_assistant.task_worker import (
    TaskWorker,
    handle_log_message,
    handle_llm_callback,
    handle_index_email,
    shutdown_event,
    new_task_event,
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
    # --- Embedding Imports ---
    EmbeddingGenerator,
    LiteLLMEmbeddingGenerator,
    SentenceTransformerEmbeddingGenerator,  # If available
    MockEmbeddingGenerator,  # For testing
)

# Import tool definitions from the new tools module
from family_assistant.tools import (
    TOOLS_DEFINITION as local_tools_definition,
    _scan_user_docs,  # Import the scanner function
    AVAILABLE_FUNCTIONS as local_tool_implementations,
    LocalToolsProvider,
    MCPToolsProvider,
    CompositeToolsProvider,
    ConfirmingToolsProvider,  # Import the class directly
    ToolExecutionContext,
    ToolsProvider,  # Import protocol for type hinting
)
from family_assistant.tools import (
    _scan_user_docs,
)  # Import the scanner function from tools package

# Import the FastAPI app
from family_assistant.web_server import app as fastapi_app

# Import storage functions
# Import facade for primary access
from family_assistant.storage import (
    init_db,
    get_all_notes,
    add_message_to_history,
    # get_all_notes, # Will be called with context
    # add_message_to_history, # Will be called with context
    # get_recent_history, # Will be called with context
    # get_message_by_id, # Will be called with context
    # add_or_update_note, # Called via tools provider
)

# Import the whole storage module for task queue functions etc.
from family_assistant import storage

# Import items specifically from storage.context
from family_assistant.storage.context import (
    DatabaseContext,  # Add back DatabaseContext
    get_db_context,  # Add back get_db_context
)

# Import calendar functions
from family_assistant import calendar_integration

# Import the Telegram service class
from .telegram_bot import TelegramService  # Updated import

# Import indexing components
from family_assistant.indexing.email_indexer import (
    handle_index_email,
    set_indexing_dependencies,
)
from family_assistant.indexing.document_indexer import (
    DocumentIndexer,
)  # Import the class

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
CONFIG_FILE_PATH = "config.yaml"  # Path to the new config file


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
        "allowed_user_ids": [],
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
        "tools_requiring_confirmation": [], # Default empty list
        "mcp_config": {"mcpServers": {}},  # Default empty MCP config
        "server_url": "http://localhost:8000",  # Default server URL
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
                logger.warning(
                    f"Config file {config_file_path} is not a valid dictionary. Ignoring."
                )
    except FileNotFoundError:
        logger.warning(f"{config_file_path} not found. Using default configurations.")
    except yaml.YAMLError as e:
        logger.error(f"Error parsing {config_file_path}: {e}. Using previous defaults.")

    # 3. Load Environment Variables (overriding config file)
    load_dotenv()  # Load .env file if present

    # Secrets (should ONLY come from env)
    config_data["telegram_token"] = os.getenv(
        "TELEGRAM_BOT_TOKEN", config_data["telegram_token"]
    )
    config_data["openrouter_api_key"] = os.getenv(
        "OPENROUTER_API_KEY", config_data["openrouter_api_key"]
    )
    config_data["database_url"] = os.getenv("DATABASE_URL", config_data["database_url"])

    # Other Env Vars
    config_data["model"] = os.getenv("LLM_MODEL", config_data["model"])
    config_data["embedding_model"] = os.getenv(
        "EMBEDDING_MODEL", config_data["embedding_model"]
    )
    config_data["embedding_dimensions"] = int(
        os.getenv("EMBEDDING_DIMENSIONS", str(config_data["embedding_dimensions"]))
    )
    config_data["timezone"] = os.getenv("TIMEZONE", config_data["timezone"])
    config_data["server_url"] = os.getenv("SERVER_URL", config_data["server_url"])  # Load SERVER_URL
    config_data["litellm_debug"] = os.getenv(
        "LITELLM_DEBUG", str(config_data["litellm_debug"])
    ).lower() in ("true", "1", "yes")

    # Parse comma-separated lists from Env Vars
    allowed_ids_str = os.getenv("ALLOWED_USER_IDS", os.getenv("ALLOWED_CHAT_IDS"))
    if allowed_ids_str is not None:  # Only override if env var is explicitly set
        try:
            config_data["allowed_user_ids"] = [
                int(cid.strip()) for cid in allowed_ids_str.split(",") if cid.strip()
            ]
        except ValueError:
            logger.error(
                "Invalid format for ALLOWED_USER_IDS env var. Using previous value."
            )

    dev_id_str = os.getenv("DEVELOPER_CHAT_ID")
    if dev_id_str is not None:  # Only override if env var is explicitly set
        try:
            config_data["developer_chat_id"] = int(dev_id_str)
        except ValueError:
            logger.error("Invalid DEVELOPER_CHAT_ID env var. Using previous value.")

    # Tools requiring confirmation from Env Var (comma-separated list)
    tools_confirm_str_env = os.getenv("TOOLS_REQUIRING_CONFIRMATION")
    if tools_confirm_str_env is not None: # Only override if env var is explicitly set
        # Override the list loaded from config.yaml
        config_data["tools_requiring_confirmation"] = [
            tool.strip() for tool in tools_confirm_str_env.split(",") if tool.strip()
        ]
        logger.info("Loaded tools requiring confirmation from environment variable.")

    # Calendar Config from Env Vars (overrides anything in config.yaml for calendars)
    caldav_user_env = os.getenv("CALDAV_USERNAME")
    caldav_pass_env = os.getenv("CALDAV_PASSWORD")
    caldav_urls_str_env = os.getenv("CALDAV_CALENDAR_URLS")

    temp_calendar_config = {}
    if caldav_user_env and caldav_pass_env and caldav_urls_str_env:
        caldav_urls_env = [
            url.strip() for url in caldav_urls_str_env.split(",") if url.strip()
        ]
        if caldav_urls_env:
            temp_calendar_config["caldav"] = {
                "username": caldav_user_env,
                "password": caldav_pass_env,
                "calendar_urls": caldav_urls_env,
            }
            logger.info("Loaded CalDAV config from environment variables.")

    ical_urls_str_env = os.getenv("ICAL_URLS")
    if ical_urls_str_env:
        ical_urls_env = [
            url.strip() for url in ical_urls_str_env.split(",") if url.strip()
        ]
        if ical_urls_env:
            temp_calendar_config["ical"] = {"urls": ical_urls_env}
            logger.info("Loaded iCal config from environment variables.")

    # Only update calendar_config if env vars provided valid config
    if temp_calendar_config:
        config_data["calendar_config"] = temp_calendar_config
    elif not config_data.get("calendar_config"):  # If no config from yaml either
        logger.warning(
            "No calendar sources configured in config file or environment variables."
        )

    # Validate Timezone
    try:
        zoneinfo.ZoneInfo(config_data["timezone"])
    except zoneinfo.ZoneInfoNotFoundError:
        logger.error(
            f"Invalid timezone '{config_data['timezone']}'. Defaulting to UTC."
        )
        config_data["timezone"] = "UTC"

    # 4. Load other config files (Prompts, MCP)
    # Load prompts from YAML file
    try:
        with open("prompts.yaml", "r", encoding="utf-8") as f:
            loaded_prompts = yaml.safe_load(f)
            if isinstance(loaded_prompts, dict):
                config_data["prompts"] = loaded_prompts  # Store in config dict
                logger.info("Successfully loaded prompts from prompts.yaml")
            else:
                logger.error(
                    "Failed to load prompts: prompts.yaml is not a valid dictionary."
                )
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
                config_data["mcp_config"] = loaded_mcp_config  # Store in config dict
                logger.info(f"Successfully loaded MCP config from {mcp_config_path}")
            else:
                logger.error(
                    f"Failed to load MCP config: {mcp_config_path} is not a valid dictionary."
                )
    except FileNotFoundError:
        logger.info(f"{mcp_config_path} not found. MCP features may be disabled.")
    except json.JSONDecodeError as e:
        logger.error(f"Error decoding {mcp_config_path}: {e}")

    # Log final loaded non-secret config for verification
    loggable_config = copy.deepcopy(
        {
            k: v
            for k, v in config_data.items()
            if k
            not in [
                "telegram_token",
                "openrouter_api_key",
                "database_url",
            ]  # Exclude secrets
        }
    )
    if (
        "calendar_config" in loggable_config
        and "caldav" in loggable_config["calendar_config"]
    ):
        loggable_config["calendar_config"]["caldav"].pop(
            "password", None
        )  # Remove password before logging
    logger.info(
        f"Final configuration loaded (excluding secrets): {json.dumps(loggable_config, indent=2, default=str)}"
    )

    return config_data


# --- MCP Configuration Loading & Connection --- (REMOVED - Handled by MCPToolsProvider)
# async def load_mcp_config_and_connect(mcp_config: Dict[str, Any]):
#    ... (Removed function body) ...


# --- Argument Parsing ---
# Define parser here, but parse arguments later in main() after loading config
parser = argparse.ArgumentParser(description="Family Assistant Bot")
parser.add_argument(
    "--telegram-token",
    default=None,  # Default is None, will be loaded from env/config
    help="Telegram Bot Token (overrides environment variable)",
)
parser.add_argument(
    "--openrouter-api-key",
    default=None,  # Default is None, will be loaded from env/config
    help="OpenRouter API Key (overrides environment variable)",
)
parser.add_argument(
    "--model",
    default=None,  # Default is None, will be loaded from env/config
    help="LLM model identifier (overrides config file and environment variable)",
)
parser.add_argument(
    "--embedding-model",
    default=None,  # Default is None, will be loaded from env/config
    help="Embedding model identifier (overrides config file and environment variable)",
)
parser.add_argument(
    "--embedding-dimensions",
    type=int,
    default=None,  # Default is None, will be loaded from env/config
    help="Embedding model dimensionality (overrides config file and environment variable)",
)
# Add argument for config file path?
# parser.add_argument('--config', default='config.yaml', help='Path to the main YAML configuration file.')


# --- Signal Handlers ---
# Define handlers before main() where they are registered
# Add mcp_provider argument
async def shutdown_handler(
    signal_name: str,
    telegram_service: Optional[TelegramService],
    tools_provider: Optional[
        ToolsProvider
    ],  # Use generic ToolsProvider and correct name
):
    """Initiates graceful shutdown."""
    logger.warning(f"Received signal {signal_name}. Initiating shutdown...")
    # Ensure the event is set to signal other parts of the application
    if not shutdown_event.is_set():  # Check before setting
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

    # Close tool providers via the generic interface
    if tools_provider:
        logger.info("Closing tool providers...")
        await tools_provider.close()  # Call close on the top-level provider
        logger.info("Tool providers closed.")
    else:
        logger.warning("ToolsProvider instance was None during shutdown.")

    # Telegram application shutdown is now handled within telegram_service.stop_polling()


def reload_config_handler(signum, frame):
    """Handles SIGHUP for config reloading (placeholder)."""
    logger.info("Received SIGHUP signal. Reloading configuration...")
    load_config()
    # Potentially restart parts of the application if needed,
    # but be careful with state. For now, just log and reload vars.


# --- Main Application Setup & Run ---
# Return tuple: (TelegramService, ToolsProvider) or (None, None)
async def main_async(
    config: Dict[str, Any],  # Accept the resolved config dictionary
) -> Tuple[
    Optional[TelegramService], Optional[ToolsProvider]
]:  # Return generic ToolsProvider
    """Initializes and runs the bot application using the provided configuration."""
    # global application # Removed
    logger.info(f"Using model: {config['model']}")
    # mcp_provider = None # No longer needed, composite provider is the main one

    # --- Validate Essential Config ---
    # Secrets should have been loaded by load_config() from env vars
    if not config.get("telegram_token"):
        raise ValueError(
            "Telegram Bot Token is missing (check env var TELEGRAM_BOT_TOKEN)."
        )
    if not config.get("openrouter_api_key"):
        raise ValueError(
            "OpenRouter API Key is missing (check env var OPENROUTER_API_KEY)."
        )

    # Set OpenRouter API key for LiteLLM (if using LiteLLMClient)
    # This might be redundant if LiteLLM picks it up automatically, but explicit is okay.
    os.environ["OPENROUTER_API_KEY"] = config["openrouter_api_key"]

    # --- LLM Client Instantiation ---
    llm_client: LLMInterface = LiteLLMClient(
        model=config["model"],
        model_parameters=config.get("llm_parameters", {}),  # Get from config dict
    )

    # --- Embedding Generator Instantiation ---
    embedding_generator: EmbeddingGenerator
    embedding_model_name = config["embedding_model"]
    embedding_dimensions = config["embedding_dimensions"]
    # Example check for local model (adjust condition as needed)
    if embedding_model_name.startswith("/") or embedding_model_name in [
        "all-MiniLM-L6-v2",
        "other-local-model-name",
    ]:
        try:
            if "SentenceTransformerEmbeddingGenerator" not in dir(embeddings):
                raise ImportError(
                    "sentence-transformers library not installed, cannot use local embedding model."
                )
            embedding_generator = embeddings.SentenceTransformerEmbeddingGenerator(
                model_name_or_path=embedding_model_name
                # Pass dimensions if the local model class supports it? Check SentenceTransformer docs.
            )
        except (ImportError, ValueError, RuntimeError) as local_embed_err:
            logger.critical(
                f"Failed to initialize local embedding model '{embedding_model_name}': {local_embed_err}"
            )
            raise SystemExit(
                f"Local embedding model initialization failed: {local_embed_err}"
            )
    else:
        # Assume API-based model via LiteLLM
        embedding_generator = LiteLLMEmbeddingGenerator(
            model=embedding_model_name,
            dimensions=embedding_dimensions,  # Pass dimensions from config
        )

    logger.info(
        f"Using embedding generator: {type(embedding_generator).__name__} with model: {embedding_generator.model_name}"
    )

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
        await storage.init_vector_db(db_ctx)  # Initialize vector specific parts

    # Load MCP config and connect to servers using config dict (REMOVED - Handled by provider)
    # await load_mcp_config_and_connect(config["mcp_config"]) # Pass MCP config part

    # --- Instantiate Tool Providers ---
    # Scan for documentation files and update the relevant tool definition
    available_doc_files = _scan_user_docs()
    formatted_doc_list = ", ".join(available_doc_files) or "None"
    updated_local_tools_definition = copy.deepcopy(
        local_tools_definition
    )  # Avoid modifying original
    for tool_def in updated_local_tools_definition:
        if tool_def.get("function", {}).get("name") == "get_user_documentation_content":
            try:
                tool_def["function"]["description"] = tool_def["function"][
                    "description"
                ].format(available_doc_files=formatted_doc_list)
                logger.info(
                    f"Updated 'get_user_documentation_content' description with files: {formatted_doc_list}"
                )
            except KeyError as e:
                logger.error(
                    f"Failed to format documentation tool description: {e}",
                    exc_info=True,
                )
    local_provider = LocalToolsProvider(
        definitions=updated_local_tools_definition,  # Use the updated list
        implementations=local_tool_implementations,
        embedding_generator=embedding_generator,
        calendar_config=config["calendar_config"],  # Get from config dict
    )
    # Instantiate MCP provider, passing the server configs from the main config
    # It will connect on first use (e.g., when get_tool_definitions is called)
    mcp_provider = MCPToolsProvider(
        mcp_server_configs=config.get("mcp_config", {}).get("mcpServers", {}),
        # mcp_client=None # Optional: pass a shared mcp.Client if needed elsewhere
    )
    # Combine providers
    try:
        composite_provider = CompositeToolsProvider(
            providers=[local_provider, mcp_provider]
        )
        await composite_provider.get_tool_definitions()
        logger.info("CompositeToolsProvider initialized and validated.")
    except ValueError as provider_err:
        logger.critical(
            f"Failed to initialize CompositeToolsProvider: {provider_err}",
            exc_info=True,
        )
        raise SystemExit(f"Tool provider initialization failed: {provider_err}")

    # --- Wrap with Confirming Provider ---
    # Retrieve the list of tools requiring confirmation from the loaded config
    confirm_tool_names = set(config.get("tools_requiring_confirmation", []))
    logger.info(f"Tools configured to require confirmation: {confirm_tool_names}")

    confirming_provider = ConfirmingToolsProvider(  # Use the imported class name directly
        wrapped_provider=composite_provider,
        tools_requiring_confirmation=confirm_tool_names, # Pass the set from config
        calendar_config=config[
            "calendar_config"
        ],  # Pass calendar config for detail fetching
        # confirmation_timeout=... # Optional: Set custom timeout if needed
    )
    # Ensure the confirming provider loads its definitions (identifies tools needing confirmation)
    tool_definitions = await confirming_provider.get_tool_definitions()
    logger.info("ConfirmingToolsProvider initialized and definitions loaded.")

    # --- Store tool definitions in app state for web server access ---
    fastapi_app.state.tool_definitions = tool_definitions
    logger.info(
        f"Stored {len(tool_definitions)} tool definitions in FastAPI app state."
    )
    # --- Store the actual tool provider instance for execution ---
    fastapi_app.state.tools_provider = (
        confirming_provider  # Store the confirming provider
    )
    logger.info("Stored ToolsProvider instance in FastAPI app state.")

    # --- Instantiate Processing Service ---
    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=confirming_provider,  # Use the confirming provider wrapper
        prompts=config["prompts"],
        calendar_config=config["calendar_config"],
        timezone_str=config["timezone"],
        max_history_messages=config["max_history_messages"],
        history_max_age_hours=config["history_max_age_hours"],
        server_url=config["server_url"],  # Pass server URL
    )
    logger.info(f"ProcessingService initialized with configuration.")

    # --- Instantiate Indexers ---
    document_indexer = DocumentIndexer(embedding_generator=embedding_generator)
    set_indexing_dependencies(
        embedding_generator=embedding_generator, llm_client=llm_client
    )

    # --- Instantiate Telegram Service ---
    telegram_service = TelegramService(
        telegram_token=config["telegram_token"],
        allowed_user_ids=config["allowed_user_ids"],
        developer_chat_id=config["developer_chat_id"],
        processing_service=processing_service,
        get_db_context_func=get_db_context,
    )

    # Start polling using the service method
    await telegram_service.start_polling()

    # --- Store service in app state for web server access ---
    fastapi_app.state.telegram_service = telegram_service
    logger.info("Stored TelegramService instance in FastAPI app state.")

    # --- Uvicorn Server Setup ---
    uvicorn_config = uvicorn.Config(
        fastapi_app, host="0.0.0.0", port=8000, log_level="info"
    )
    server = uvicorn.Server(uvicorn_config)

    # Run Uvicorn server concurrently (polling is already running)
    # telegram_task = asyncio.create_task(application.updater.start_polling(allowed_updates=Update.ALL_TYPES)) # Removed duplicate start_polling
    web_server_task = asyncio.create_task(server.serve())
    logger.info("Web server running on http://0.0.0.0:8000")

    # --- Instantiate Task Worker ---
    # Ensure telegram_service and application are initialized before this
    if not telegram_service or not hasattr(telegram_service, "application"):
        logger.critical(
            "Telegram service or application not initialized before TaskWorker creation."
        )
        raise SystemExit(
            "Critical error: Cannot create TaskWorker without Telegram application."
        )

    task_worker_instance = TaskWorker(
        processing_service=processing_service,
        application=telegram_service.application,
        calendar_config=config["calendar_config"],
        timezone_str=config["timezone"],
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
    # Register LLM callback handler directly (dependencies passed via context by worker)
    task_worker_instance.register_task_handler("llm_callback", handle_llm_callback)

    logger.info(
        f"Registered task handlers for worker {task_worker_instance.worker_id}: {list(task_worker_instance.get_task_handlers().keys())}"
    )

    # Start the task queue worker using the instance's run method
    task_worker_task = asyncio.create_task(
        task_worker_instance.run(new_task_event)  # Pass the event to the run method
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
    # MCP provider cleanup is handled by shutdown_handler calling tools_provider.close()

    # Return the top-level confirming provider instance
    return telegram_service, confirming_provider


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
        logger.info(
            f"Overriding embedding model with CLI argument: {args.embedding_model}"
        )
    if args.embedding_dimensions is not None:
        config_data["embedding_dimensions"] = args.embedding_dimensions
        logger.info(
            f"Overriding embedding dimensions with CLI argument: {args.embedding_dimensions}"
        )
    # Add overrides for other CLI args if introduced (e.g., --config)

    # --- Store final config in app state for web server access ---
    fastapi_app.state.config = config_data
    logger.info("Stored final configuration dictionary in FastAPI app state.")

    # --- Event Loop and Signal Handlers ---
    loop = asyncio.get_event_loop()
    telegram_service_instance = None  # Initialize
    tools_provider_instance = None  # Use generic name

    try:
        logger.info("Starting application...")
        # Pass the final resolved config dictionary to main_async
        # Capture both returned instances
        telegram_service_instance, tools_provider_instance = loop.run_until_complete(
            main_async(config_data)
        )  # Assign to generic name

        # --- Setup Signal Handlers *after* service creation ---
        # Pass the service instance to the shutdown handler lambda
        # Always set up signal handlers, but pass None for instances if creation failed
        signal_map = {
            signal.SIGINT: "SIGINT",
            signal.SIGTERM: "SIGTERM",
        }
        # Need mcp_provider_instance here, which was returned by main_async
        # Let's assume main_async was correctly modified to return it,
        # and we need to capture it earlier in the 'try' block.
        # The previous block failed because the call to main_async wasn't updated first.
        # Assuming main_async now returns (service, mcp_provider), the call should be:
        # telegram_service_instance, mcp_provider_instance = loop.run_until_complete(main_async(config_data))

        # Corrected signal handler setup using both instances:
        for sig_num, sig_name in signal_map.items():
            # Pass service and generic tools provider instances (which might be None)
            loop.add_signal_handler(
                sig_num,
                lambda name=sig_name, service=telegram_service_instance, tools=tools_provider_instance: asyncio.create_task(
                    shutdown_handler(
                        name, service, tools
                    )  # Pass potentially None instances
                ),
            )
        if not telegram_service_instance:
            logger.error(
                "TelegramService instance creation failed, shutdown might be incomplete."
            )
        if not tools_provider_instance:  # Check generic instance
            logger.warning(
                "ToolsProvider instance creation failed or not configured."
            )  # Warning if top-level provider is None

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
            # Run the async shutdown handler within the loop, passing both instances
            loop.run_until_complete(
                shutdown_handler(
                    type(ex).__name__,
                    telegram_service_instance,
                    tools_provider_instance,
                )  # Pass generic provider
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
