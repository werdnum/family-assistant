import argparse
import asyncio
import copy
import json
import logging
import os
import signal
import sys
import types  # Add import for types
import zoneinfo
from typing import Any

import uvicorn
import yaml
from dotenv import load_dotenv

# Import Embedding interface/clients
import family_assistant.embeddings as embeddings

# Import the whole storage module for task queue functions etc.
from family_assistant import storage

# --- NEW: Import ContextProvider and its implementations ---
from family_assistant.context_providers import (
    CalendarContextProvider,
    KnownUsersContextProvider,  # Added
    NotesContextProvider,
)
from family_assistant.embeddings import (
    # --- Embedding Imports ---
    EmbeddingGenerator,
    LiteLLMEmbeddingGenerator,  # For testing
)
from family_assistant.indexing.document_indexer import (
    DocumentIndexer,
)  # Import the class

# Import indexing components
from family_assistant.indexing.email_indexer import (
    EmailIndexer,  # Changed this import
)

# Import the specific task handler for embedding
from family_assistant.indexing.tasks import handle_embed_and_store_batch
from family_assistant.llm import (
    LiteLLMClient,
    LLMInterface,
)

# Import the ProcessingService and LLM interface/clients
from family_assistant.processing import ProcessingService, ProcessingServiceConfig

# Import storage functions
# Import facade for primary access
from family_assistant.storage import (
    init_db,
    # get_all_notes, # Will be called with context
    # add_message_to_history, # Will be called with context
    # get_recent_history, # Will be called with context
    # get_message_by_id, # Will be called with context
    # add_or_update_note, # Called via tools provider
)

# Import items specifically from storage.context
from family_assistant.storage.context import (
    DatabaseContext,  # Added for type hinting and wrapper
    get_db_context,  # Add back get_db_context
)

# Import task worker CLASS, handlers, and events
from family_assistant.task_worker import (
    TaskWorker,
    handle_llm_callback,
    new_task_event,
    shutdown_event,
)
from family_assistant.task_worker import (
    handle_log_message as original_handle_log_message,  # Aliased
)
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS as local_tool_implementations,
)

# Import tool definitions from the new tools module
from family_assistant.tools import (
    TOOLS_DEFINITION as local_tools_definition,
)
from family_assistant.tools import (
    CompositeToolsProvider,
    ConfirmingToolsProvider,  # Import the class directly
    LocalToolsProvider,
    MCPToolsProvider,
    ToolsProvider,  # Import protocol for type hinting
    _scan_user_docs,  # Import the scanner function
)
from family_assistant.tools.types import ToolExecutionContext  # Added for wrapper
from family_assistant.utils.scraping import PlaywrightScraper  # Added

# Import the FastAPI app
from family_assistant.web.app_creator import app as fastapi_app

# Import calendar functions
# Import the Telegram service class
from .telegram_bot import TelegramService

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


# --- Helper Functions ---
def deep_merge_dicts(base_dict: dict, merge_dict: dict) -> dict:
    """Deeply merges merge_dict into base_dict."""
    result = copy.deepcopy(base_dict)  # Start with a deep copy of the base
    for key, value in merge_dict.items():
        if isinstance(value, dict) and key in result and isinstance(result[key], dict):
            result[key] = deep_merge_dicts(result[key], value)
        else:
            result[key] = value  # This will overwrite or add the key
    return result


# --- Constants ---
MAX_HISTORY_MESSAGES = 5  # Number of recent messages to include (excluding current)
HISTORY_MAX_AGE_HOURS = 24  # Only include messages from the last X hours
CONFIG_FILE_PATH = "config.yaml"  # Path to the new config file


# --- Configuration Loading ---
def load_config(config_file_path: str = CONFIG_FILE_PATH) -> dict[str, Any]:
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
    config_data: dict[str, Any] = {
        # --- Top-level application-wide settings & secrets placeholders ---
        "telegram_token": None,
        "openrouter_api_key": None,
        "gemini_api_key": None,
        "allowed_user_ids": [],
        "developer_chat_id": None,
        "model": "openrouter/google/gemini-2.5-pro-preview-03-25",
        "embedding_model": "gemini/gemini-embedding-exp-03-07",
        "embedding_dimensions": 1536,
        "database_url": "sqlite+aiosqlite:///family_assistant.db",
        "litellm_debug": False,
        "server_url": "http://localhost:8000",
        "document_storage_path": "/mnt/data/files",
        "attachment_storage_path": "/mnt/data/mailbox/attachments",
        "llm_parameters": {},  # Global LLM parameters
        "mcp_config": {"mcpServers": {}},  # Global MCP server definitions
        "default_service_profile_id": "default_assistant",  # Default profile ID
        "service_profiles": [],  # List of service profile configurations
        "indexing_pipeline_config": {  # Global indexing pipeline config
            "processors": [
                {"type": "TitleExtractor"},
                {"type": "PDFTextExtractor"},
                {
                    "type": "LLMPrimaryLinkExtractor",
                    "config": {
                        "input_content_types": ["raw_body_text"],
                        "target_embedding_type": "raw_url",
                    },
                },
                {"type": "WebFetcher"},
                {
                    "type": "LLMSummaryGenerator",
                    "config": {
                        "input_content_types": [
                            "original_document_file",
                            "raw_body_text",
                            "extracted_markdown_content",
                            "fetched_content_markdown",
                        ],
                        "target_embedding_type": "llm_generated_summary",
                    },
                },
                {
                    "type": "TextChunker",
                    "config": {
                        "chunk_size": 1000,
                        "chunk_overlap": 100,
                        "embedding_type_prefix_map": {
                            "raw_body_text": "content_chunk",
                            "raw_file_text": "content_chunk",
                            "extracted_markdown_content": "content_chunk",
                            "fetched_content_markdown": "content_chunk",
                        },
                    },
                },
                {
                    "type": "EmbeddingDispatch",
                    "config": {
                        "embedding_types_to_dispatch": [
                            "title",
                            "content_chunk",
                            "llm_generated_summary",
                        ]
                    },
                },
            ]
        },
        # --- Default Service Profile Settings ---
        "default_profile_settings": {
            "processing_config": {
                "prompts": {},  # Populated from prompts.yaml
                "calendar_config": {},  # Populated from CALDAV_*/ICAL_URLS env vars
                "timezone": "UTC",
                "max_history_messages": 5,
                "history_max_age_hours": 24,
                # Default LLM model for profiles, can be overridden per profile
                "llm_model": "openrouter/google/gemini-2.5-pro-preview-03-25",  # Use actual default model string
            },
            "chat_id_to_name_map": {},  # Added for known user mapping
            "tools_config": {
                # enable_local_tools and enable_mcp_server_ids are implicitly "all available"
                # for the default profile in the current setup.
                # Explicit lists can be added to config.yaml later if needed.
                "enable_local_tools": [],  # Default: empty list, meaning all available if not specified
                "enable_mcp_server_ids": [],  # Default: empty list, meaning all available if not specified
                "confirm_tools": [],  # Default empty; overridden by config.yaml or env var
            },
            "slash_commands": [],  # Default: no specific slash commands for this profile
        },
    }
    logger.info("Initialized config with code defaults.")

    # 2. Load config.yaml
    try:
        with open(config_file_path, encoding="utf-8") as f:
            yaml_config = yaml.safe_load(f)
            if isinstance(yaml_config, dict):
                # Merge YAML config. For nested dicts like default_profile_settings,
                # this needs to be a deep merge if we want to overlay.
                # For now, a simple update might mostly work if YAML defines full structures.
                # A more robust merge function would be better for partial overrides in YAML.

                # Manual deep merge for relevant sections for now
                for key, value in yaml_config.items():
                    if (
                        key == "default_profile_settings"
                        and isinstance(value, dict)
                        and isinstance(config_data.get(key), dict)
                    ):
                        # Deep merge for processing_config
                        if "processing_config" in value and isinstance(
                            value["processing_config"], dict
                        ):
                            config_data[key].setdefault("processing_config", {}).update(
                                value["processing_config"]
                            )
                        # Deep merge for tools_config
                        if "tools_config" in value and isinstance(
                            value["tools_config"], dict
                        ):
                            config_data[key].setdefault("tools_config", {}).update(
                                value["tools_config"]
                            )
                        # Deep merge for chat_id_to_name_map
                        if "chat_id_to_name_map" in value and isinstance(
                            value["chat_id_to_name_map"], dict
                        ):
                            config_data[key].setdefault(
                                "chat_id_to_name_map", {}
                            ).update(value["chat_id_to_name_map"])
                        # For other keys within default_profile_settings, direct update
                        for sub_key, sub_value in value.items():
                            if sub_key not in [
                                "processing_config",
                                "tools_config",
                                "chat_id_to_name_map",
                                "slash_commands",  # Add slash_commands here
                            ]:
                                config_data[key][sub_key] = sub_value
                        # Handle slash_commands specifically (it's a list, replace)
                        if "slash_commands" in value and isinstance(
                            value["slash_commands"], list
                        ):
                            config_data[key]["slash_commands"] = value["slash_commands"]
                    elif (
                        key in config_data
                        and isinstance(value, dict)
                        and isinstance(config_data[key], dict)
                    ):
                        config_data[key].update(value)  # Merge other top-level dicts
                    else:
                        config_data[key] = value  # Replace other top-level keys

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

    # Top-level settings from Env Vars
    config_data["default_service_profile_id"] = os.getenv(
        "DEFAULT_SERVICE_PROFILE_ID", config_data["default_service_profile_id"]
    )

    # Secrets (should ONLY come from env)
    config_data["telegram_token"] = os.getenv(
        "TELEGRAM_BOT_TOKEN", config_data["telegram_token"]
    )
    # Allow API keys to be None if not set in env, they are validated later
    config_data["openrouter_api_key"] = os.getenv(
        "OPENROUTER_API_KEY", config_data.get("openrouter_api_key")
    )
    config_data["gemini_api_key"] = os.getenv(
        "GEMINI_API_KEY", config_data.get("gemini_api_key")
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
    # --- Target nested config for profile-specific settings ---
    profile_settings = config_data["default_profile_settings"]  # Get the whole profile
    profile_proc_config = profile_settings["processing_config"]
    profile_tools_config = profile_settings["tools_config"]

    profile_proc_config["timezone"] = os.getenv(
        "TIMEZONE", profile_proc_config["timezone"]
    )
    config_data["server_url"] = os.getenv(
        "SERVER_URL", config_data["server_url"]
    )  # Load SERVER_URL
    config_data["document_storage_path"] = os.getenv(
        "DOCUMENT_STORAGE_PATH", config_data["document_storage_path"]
    )
    config_data["attachment_storage_path"] = os.getenv(  # Load ATTACHMENT_STORAGE_PATH
        "ATTACHMENT_STORAGE_PATH", config_data["attachment_storage_path"]
    )
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
    if tools_confirm_str_env is not None:  # Only override if env var is explicitly set
        # Override the list loaded from config.yaml
        profile_tools_config["confirm_tools"] = [
            tool.strip() for tool in tools_confirm_str_env.split(",") if tool.strip()
        ]
        logger.info(
            "Loaded tools requiring confirmation from environment variable into profile."
        )

    # Chat ID to Name Map from Env Var (comma-separated key:value pairs)
    chat_id_map_str_env = os.getenv("CHAT_ID_TO_NAME_MAP")
    if chat_id_map_str_env is not None:
        try:
            parsed_map = {}
            pairs = chat_id_map_str_env.split(",")
            for pair in pairs:
                if ":" in pair:
                    chat_id_str, name = pair.split(":", 1)
                    parsed_map[int(chat_id_str.strip())] = name.strip()
            profile_settings["chat_id_to_name_map"] = parsed_map
            logger.info(
                f"Loaded chat_id_to_name_map from environment variable into profile: {parsed_map}"
            )
        except ValueError as e:
            logger.error(
                f"Invalid format for CHAT_ID_TO_NAME_MAP env var: {e}. Using previous value. Expected format: '123:Alice,456:Bob'"
            )

    # Calendar Config from Env Vars (overrides anything in config.yaml for calendars)
    # This will populate default_profile_settings.processing_config.calendar_config
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

    # Only update profile's calendar_config if env vars provided valid config
    if temp_calendar_config:
        profile_proc_config["calendar_config"] = temp_calendar_config
    elif not profile_proc_config.get(
        "calendar_config"
    ):  # If no config from yaml either
        logger.warning(
            "No calendar sources configured for default profile in config file or environment variables."
        )

    # Validate Timezone in the profile
    try:
        zoneinfo.ZoneInfo(profile_proc_config["timezone"])
    except zoneinfo.ZoneInfoNotFoundError:
        logger.error(
            f"Invalid timezone '{profile_proc_config['timezone']}' in profile. Defaulting to UTC."
        )
        profile_proc_config["timezone"] = "UTC"

    # 4. Load other config files (Prompts, MCP)
    # Load prompts from YAML file into the profile's prompts
    try:
        with open("prompts.yaml", encoding="utf-8") as f:
            loaded_prompts = yaml.safe_load(f)
            if isinstance(loaded_prompts, dict):
                profile_proc_config["prompts"] = loaded_prompts
                logger.info(
                    "Successfully loaded prompts from prompts.yaml into profile."
                )
            else:
                logger.error(
                    "Failed to load prompts: prompts.yaml is not a valid dictionary."
                )
    except FileNotFoundError:
        logger.error("prompts.yaml not found. Profile using default prompt structures.")
    except yaml.YAMLError as e:
        logger.error(f"Error parsing prompts.yaml for profile: {e}")

    # --- Resolve Service Profiles ---
    # Start with profiles loaded from YAML (or empty list if not in YAML)
    yaml_service_profiles = config_data.get("service_profiles", [])
    if not isinstance(yaml_service_profiles, list):
        logger.warning(
            f"Service profiles in {config_file_path} is not a list. Ignoring."
        )
        yaml_service_profiles = []

    resolved_service_profiles = []
    default_settings = config_data["default_profile_settings"]

    for profile_def in yaml_service_profiles:
        if not isinstance(profile_def, dict) or "id" not in profile_def:
            logger.warning(
                f"Invalid profile definition in {config_file_path} (missing ID or not a dict): {profile_def}. Skipping."
            )
            continue

        # Start with a deep copy of default_profile_settings
        resolved_profile_config = copy.deepcopy(default_settings)
        resolved_profile_config["id"] = profile_def["id"]  # Ensure ID is set
        resolved_profile_config["description"] = profile_def.get("description", "")

        # Merge processing_config
        if "processing_config" in profile_def and isinstance(
            profile_def["processing_config"], dict
        ):
            # Deep merge for 'prompts' and 'calendar_config'
            if "prompts" in profile_def["processing_config"]:
                resolved_profile_config["processing_config"]["prompts"] = (
                    deep_merge_dicts(
                        resolved_profile_config["processing_config"].get("prompts", {}),
                        profile_def["processing_config"]["prompts"],
                    )
                )
            if "calendar_config" in profile_def["processing_config"]:
                resolved_profile_config["processing_config"]["calendar_config"] = (
                    deep_merge_dicts(
                        resolved_profile_config["processing_config"].get(
                            "calendar_config", {}
                        ),
                        profile_def["processing_config"]["calendar_config"],
                    )
                )
            # Replace for scalar values like llm_model, timezone, max_history, history_max_age
            for scalar_key in [
                "llm_model",
                "timezone",
                "max_history_messages",
                "history_max_age_hours",
                "delegation_security_level",  # Add delegation_security_level here
            ]:
                if scalar_key in profile_def["processing_config"]:
                    resolved_profile_config["processing_config"][scalar_key] = (
                        profile_def["processing_config"][scalar_key]
                    )

        # Replace tools_config entirely if defined in profile
        if "tools_config" in profile_def and isinstance(
            profile_def["tools_config"], dict
        ):
            # For lists within tools_config, the profile's list replaces the default.
            # For other keys, it's a direct update (shallow merge for tools_config itself).
            resolved_profile_config["tools_config"] = profile_def["tools_config"]

        # Merge chat_id_to_name_map (this is outside processing_config/tools_config in default_settings)
        if "chat_id_to_name_map" in profile_def and isinstance(
            profile_def["chat_id_to_name_map"], dict
        ):
            resolved_profile_config["chat_id_to_name_map"] = deep_merge_dicts(
                resolved_profile_config.get("chat_id_to_name_map", {}),
                profile_def["chat_id_to_name_map"],
            )

        # Handle slash_commands for the profile (replace if present)
        if "slash_commands" in profile_def and isinstance(
            profile_def["slash_commands"], list
        ):
            resolved_profile_config["slash_commands"] = profile_def["slash_commands"]
        # If not in profile_def, it will retain the default (empty list from deepcopy of default_settings)

        resolved_service_profiles.append(resolved_profile_config)

    # If no profiles were defined in YAML, create a default one
    if not resolved_service_profiles:
        logger.info(
            "No service profiles defined in YAML. Creating a default profile using 'default_profile_settings'."
        )
        default_profile_entry = copy.deepcopy(default_settings)
        default_profile_entry["id"] = config_data["default_service_profile_id"]
        default_profile_entry["description"] = "Default assistant profile."
        # Ensure the llm_model from default_profile_settings.processing_config is used,
        # or fall back to global if not set there.
        if "llm_model" not in default_profile_entry["processing_config"]:
            default_profile_entry["processing_config"]["llm_model"] = config_data[
                "model"
            ]

        resolved_service_profiles.append(default_profile_entry)

    config_data["service_profiles"] = resolved_service_profiles
    logger.info(
        f"Resolved {len(resolved_service_profiles)} service profiles. Default ID: {config_data['default_service_profile_id']}"
    )

    # Load MCP config from JSON file (remains top-level in config_data)
    mcp_config_path = "mcp_config.json"
    try:
        with open(mcp_config_path, encoding="utf-8") as f:
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

    # Indexing pipeline config from environment (overrides YAML)
    indexing_pipeline_config_env = os.getenv("INDEXING_PIPELINE_CONFIG_JSON")
    if indexing_pipeline_config_env:
        try:
            loaded_env_pipeline_config = json.loads(indexing_pipeline_config_env)
            if isinstance(loaded_env_pipeline_config, dict):
                config_data["indexing_pipeline_config"] = loaded_env_pipeline_config
                logger.info(
                    "Loaded indexing_pipeline_config from environment variable."
                )
            else:
                logger.warning(
                    "INDEXING_PIPELINE_CONFIG_JSON from env is not a valid dictionary. Using previous value."
                )
        except json.JSONDecodeError as e:
            logger.error(
                f"Error parsing INDEXING_PIPELINE_CONFIG_JSON from env: {e}. Using previous value."
            )

    # Log final loaded non-secret config for verification
    loggable_config = copy.deepcopy({
        k: v
        for k, v in config_data.items()
        if k
        not in [
            "telegram_token",
            "openrouter_api_key",
            "gemini_api_key",
            "database_url",
        ]  # Exclude top-level secrets
    })
    # Also exclude password from calendar_config within default_profile_settings
    if "default_profile_settings" in loggable_config:
        profile_log_config = loggable_config["default_profile_settings"]
        if (
            "processing_config" in profile_log_config
            and "calendar_config" in profile_log_config["processing_config"]
            and "caldav" in profile_log_config["processing_config"]["calendar_config"]
        ):
            profile_log_config["processing_config"]["calendar_config"]["caldav"].pop(
                "password", None
            )

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
parser.add_argument(
    "--document-storage-path",
    default=None,
    help="Path to store uploaded documents (overrides config file and environment variable)",
)
parser.add_argument(
    "--attachment-storage-path",
    default=None,
    help="Path to store email attachments (overrides config file and environment variable)",
)


# --- Signal Handlers ---
# Define handlers before main() where they are registered
# Add mcp_provider argument
async def shutdown_handler(
    signal_name: str,
    telegram_service: TelegramService | None,
    tools_provider: ToolsProvider | None,  # Use generic ToolsProvider and correct name
) -> None:
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

    # Close tool providers from all services in the registry
    if fastapi_app.state.processing_services and isinstance(
        fastapi_app.state.processing_services, dict
    ):
        logger.info(
            f"Closing tool providers for {len(fastapi_app.state.processing_services)} services..."
        )
        for (
            profile_id,
            service_instance,
        ) in fastapi_app.state.processing_services.items():
            if (
                hasattr(service_instance, "tools_provider")
                and service_instance.tools_provider
            ):
                try:
                    logger.info(f"Closing tools_provider for profile '{profile_id}'...")
                    await service_instance.tools_provider.close()
                    logger.info(f"Tools_provider for profile '{profile_id}' closed.")
                except Exception as e:
                    logger.error(
                        f"Error closing tools_provider for profile '{profile_id}': {e}",
                        exc_info=True,
                    )
            else:
                logger.warning(
                    f"No tools_provider found for profile '{profile_id}' to close."
                )
        logger.info("All registered tool providers closed.")
    elif (
        tools_provider
    ):  # Fallback for old logic if registry not populated (should not happen)
        logger.warning(
            "Processing services registry not found or not a dict, attempting to close single tools_provider."
        )
        await tools_provider.close()
        logger.info("Single tools_provider closed.")
    else:
        logger.warning("No ToolProviders found to close during shutdown.")

    # Telegram application shutdown is now handled within telegram_service.stop_polling()


def reload_config_handler(signum: int, frame: types.FrameType | None) -> None:
    """Handles SIGHUP for config reloading (placeholder)."""
    logger.info("Received SIGHUP signal. Reloading configuration...")
    load_config()
    # Potentially restart parts of the application if needed,
    # but be careful with state. For now, just log and reload vars.


# --- Wrapper Functions for Type Compatibility ---
async def async_get_db_context_for_provider() -> DatabaseContext:
    """Wraps get_db_context to be an awaitable returning DatabaseContext for providers."""
    return get_db_context()


async def task_wrapper_handle_log_message(
    exec_context: ToolExecutionContext, payload: Any
) -> None:
    """
    Wrapper for the original handle_log_message to match TaskWorker's expected handler signature.
    It extracts db_context from ToolExecutionContext and ensures payload is a dict.
    """
    if not isinstance(payload, dict):
        logger.error(
            f"Payload for handle_log_message task is not a dict: {type(payload)}. Content: {payload}"
        )
        return  # Or raise an error, depending on desired behavior
    await original_handle_log_message(exec_context.db_context, payload)


# --- Main Application Setup & Run ---
# Return tuple: (TelegramService, ToolsProvider) or (None, None)
async def main_async(
    config: dict[str, Any],  # Accept the resolved config dictionary
) -> tuple[
    TelegramService | None, ToolsProvider | None
]:  # Return generic ToolsProvider
    """Initializes and runs the bot application using the provided configuration."""
    logger.info(f"Using model: {config['model']}")
    # --- Validate Essential Config ---
    if not config.get("telegram_token"):
        raise ValueError(
            "Telegram Bot Token is missing (check env var TELEGRAM_BOT_TOKEN)."
        )

    # API Key Validation based on selected model
    selected_model = config.get("model", "")
    if selected_model.startswith("gemini/"):
        # For Gemini, litellm primarily uses GEMINI_API_KEY from the environment.
        # config.get("gemini_api_key") would be if it was loaded into config_data and intended for direct use.
        if not os.getenv("GEMINI_API_KEY"):
            raise ValueError(
                "Gemini API Key is missing. Please set the GEMINI_API_KEY environment variable."
            )
        logger.info(
            "Gemini model selected. Will use GEMINI_API_KEY from environment for LiteLLM."
        )
    elif selected_model.startswith("openrouter/"):
        if not config.get("openrouter_api_key"):
            raise ValueError(
                "OpenRouter API Key is missing (check env var OPENROUTER_API_KEY or config file)."
            )
        # Set OpenRouter API key for LiteLLM if it's managed via config_data.
        # LiteLLM also checks for OPENROUTER_API_KEY env var independently.
        if config.get("openrouter_api_key"):
            os.environ["OPENROUTER_API_KEY"] = config["openrouter_api_key"]
        logger.info(
            "OpenRouter model selected. OPENROUTER_API_KEY will be used by LiteLLM."
        )
    # Add elif blocks for other providers like "openai/" -> OPENAI_API_KEY if needed
    else:
        logger.warning(
            f"No specific API key validation implemented for model: {selected_model}. "
            "Ensure necessary API keys (e.g., OPENAI_API_KEY, ANTHROPIC_API_KEY) are set in the environment if required by the model provider for LiteLLM."
        )

    # --- Embedding Generator Instantiation (Global for now) ---
    # TODO: Consider if embedding generator should also be per-profile if models differ significantly
    embedding_generator: EmbeddingGenerator
    embedding_model_name = config["embedding_model"]
    embedding_dimensions = config["embedding_dimensions"]

    if embedding_model_name == "mock-deterministic-embedder":
        logger.info(
            f"Using MockEmbeddingGenerator (deterministic) for model: {embedding_model_name}"
        )
        # Ensure MockEmbeddingGenerator is imported from family_assistant.embeddings
        embedding_generator = embeddings.MockEmbeddingGenerator(
            model_name=embedding_model_name,
            dimensions=embedding_dimensions,
            default_embedding_behavior="generate",  # Generate deterministic vectors for unknown texts
        )
    # Example check for local model (adjust condition as needed)
    elif embedding_model_name.startswith("/") or embedding_model_name in [
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
            ) from local_embed_err
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
    # fastapi_app.state.llm_client will be set to the default profile's LLM client later

    # Initialize database schema first
    # init_db uses the engine configured in storage/base.py, which reads DATABASE_URL env var.
    # No need to pass database_url here.
    await init_db()
    # Initialize vector DB components (extension, indexes)
    # get_db_context uses the engine configured in storage/base.py by default.
    async with get_db_context() as db_ctx:
        await storage.init_vector_db(db_ctx)  # Initialize vector specific parts

    # --- Instantiate Processing Services based on Profiles ---
    resolved_profiles = config.get("service_profiles", [])
    default_service_profile_id = config.get(
        "default_service_profile_id", "default_assistant"
    )
    processing_services_registry: dict[str, ProcessingService] = {}

    # Scan for documentation files once, globally.
    # The description will be formatted for each LocalToolsProvider instance if the tool is enabled.
    available_doc_files = _scan_user_docs()
    formatted_doc_list_for_tool_desc = ", ".join(available_doc_files) or "None"

    # Global tool definitions and implementations (to be filtered per profile)
    # Make a deepcopy of the global definitions to avoid modifying it in place during formatting.
    base_local_tools_definition = copy.deepcopy(local_tools_definition)

    for profile_conf in resolved_profiles:
        profile_id = profile_conf["id"]
        logger.info(f"Initializing ProcessingService for profile ID: '{profile_id}'")

        profile_proc_conf_dict = profile_conf["processing_config"]
        profile_tools_conf_dict = profile_conf["tools_config"]
        profile_chat_id_map = profile_conf.get("chat_id_to_name_map", {})

        # LLM Client for this profile
        profile_llm_model = profile_proc_conf_dict.get("llm_model", config["model"])
        llm_client_for_profile: LLMInterface = LiteLLMClient(
            model=profile_llm_model,
            model_parameters=config.get(
                "llm_parameters", {}
            ),  # Global LLM params for now
        )
        logger.info(f"Profile '{profile_id}' using LLM model: {profile_llm_model}")

        # ToolsProvider stack for this profile
        # Filter local tools
        enabled_local_tool_names = set(
            profile_tools_conf_dict.get("enable_local_tools", [])
        )
        if (
            not enabled_local_tool_names
        ):  # If list is empty, enable all local tools by default
            enabled_local_tool_names = set(local_tool_implementations.keys())

        profile_specific_local_definitions = []
        for (
            tool_def_template
        ) in base_local_tools_definition:  # Iterate over the global template
            tool_name = tool_def_template.get("function", {}).get("name")
            if tool_name in enabled_local_tool_names:
                # Create a copy to modify description for this profile's provider
                current_tool_def = copy.deepcopy(tool_def_template)
                if tool_name == "get_user_documentation_content":
                    try:
                        current_tool_def["function"]["description"] = current_tool_def[
                            "function"
                        ]["description"].format(
                            available_doc_files=formatted_doc_list_for_tool_desc
                        )
                    except KeyError as e:
                        logger.error(
                            f"Failed to format doc tool description for profile {profile_id}: {e}"
                        )
                profile_specific_local_definitions.append(current_tool_def)

        profile_local_implementations = {
            name: func
            for name, func in local_tool_implementations.items()
            if name in enabled_local_tool_names
        }

        local_provider_for_profile = LocalToolsProvider(
            definitions=profile_specific_local_definitions,
            implementations=profile_local_implementations,
            embedding_generator=embedding_generator,  # Global embedding generator
            calendar_config=profile_proc_conf_dict["calendar_config"],
        )

        # Filter MCP tools
        enabled_mcp_server_ids = set(
            profile_tools_conf_dict.get("enable_mcp_server_ids", [])
        )
        all_mcp_servers_config = config.get("mcp_config", {}).get("mcpServers", {})
        if (
            not enabled_mcp_server_ids and all_mcp_servers_config
        ):  # If list is empty, enable all mcp servers
            enabled_mcp_server_ids = set(all_mcp_servers_config.keys())

        profile_mcp_servers_config = {
            server_id: server_conf
            for server_id, server_conf in all_mcp_servers_config.items()
            if server_id in enabled_mcp_server_ids
        }
        mcp_provider_for_profile = MCPToolsProvider(
            mcp_server_configs=profile_mcp_servers_config,
        )

        composite_provider_for_profile = CompositeToolsProvider(
            providers=[local_provider_for_profile, mcp_provider_for_profile]
        )
        # Validate composite provider for this profile
        try:
            await (
                composite_provider_for_profile.get_tool_definitions()
            )  # This also validates
        except ValueError as provider_err:
            logger.critical(
                f"Failed to initialize CompositeToolsProvider for profile '{profile_id}': {provider_err}",
                exc_info=True,
            )
            raise SystemExit(
                f"Tool provider initialization failed for profile '{profile_id}': {provider_err}"
            ) from provider_err

        profile_confirm_tools_set = set(
            profile_tools_conf_dict.get("confirm_tools", [])
        )
        confirming_provider_for_profile = ConfirmingToolsProvider(
            wrapped_provider=composite_provider_for_profile,
            tools_requiring_confirmation=profile_confirm_tools_set,
            calendar_config=profile_proc_conf_dict["calendar_config"],
        )
        # Ensure definitions are loaded for the confirming provider
        await confirming_provider_for_profile.get_tool_definitions()

        # Context Providers for this profile
        notes_provider_for_profile = NotesContextProvider(
            get_db_context_func=async_get_db_context_for_provider,
            prompts=profile_proc_conf_dict["prompts"],
        )
        calendar_provider_for_profile = CalendarContextProvider(
            calendar_config=profile_proc_conf_dict["calendar_config"],
            timezone_str=profile_proc_conf_dict["timezone"],
            prompts=profile_proc_conf_dict["prompts"],
        )
        known_users_provider_for_profile = KnownUsersContextProvider(
            chat_id_to_name_map=profile_chat_id_map,
            prompts=profile_proc_conf_dict["prompts"],
        )
        context_providers_for_profile = [
            notes_provider_for_profile,
            calendar_provider_for_profile,
            known_users_provider_for_profile,
        ]

        # ProcessingServiceConfig for this profile
        service_config_for_profile = ProcessingServiceConfig(
            prompts=profile_proc_conf_dict["prompts"],
            calendar_config=profile_proc_conf_dict["calendar_config"],
            timezone_str=profile_proc_conf_dict["timezone"],
            max_history_messages=profile_proc_conf_dict["max_history_messages"],
            history_max_age_hours=profile_proc_conf_dict["history_max_age_hours"],
            tools_config=profile_tools_conf_dict,  # Pass the whole tools_config for this profile
            delegation_security_level=profile_proc_conf_dict.get(
                "delegation_security_level", "confirm"
            ),  # Get with a default
            id=profile_id,  # Pass the profile_id
        )

        # ProcessingService instance for this profile
        processing_service_instance = ProcessingService(
            llm_client=llm_client_for_profile,
            tools_provider=confirming_provider_for_profile,
            service_config=service_config_for_profile,
            context_providers=context_providers_for_profile,
            server_url=config["server_url"],  # Global server URL
            app_config=config,  # Global app config
        )
        processing_services_registry[profile_id] = processing_service_instance
        logger.info(
            f"ProcessingService for profile '{profile_id}' initialized successfully."
        )

    if not processing_services_registry:
        logger.critical("No processing service profiles could be initialized. Exiting.")
        raise SystemExit("No processing service profiles initialized.")

    # Inject the full registry into each service instance
    for service_instance_in_registry in processing_services_registry.values():
        service_instance_in_registry.set_processing_services_registry(
            processing_services_registry
        )
    logger.info("Injected full service registry into each ProcessingService instance.")

    fastapi_app.state.processing_services = processing_services_registry
    logger.info(
        f"Stored {len(processing_services_registry)} processing services in FastAPI app state."
    )

    # Get the default processing service
    default_processing_service = processing_services_registry.get(
        default_service_profile_id
    )
    if not default_processing_service:
        # Fallback to the first available profile if default_service_profile_id is not found
        logger.warning(
            f"Default service profile ID '{default_service_profile_id}' not found in registry. "
            f"Falling back to the first available profile: '{next(iter(processing_services_registry.keys()))}'."
        )
        default_processing_service = next(iter(processing_services_registry.values()))
        # Update default_service_profile_id to the one actually being used as default
        default_service_profile_id = next(iter(processing_services_registry.keys()))

    # Store default service, its LLM client, tools provider and definitions in app state for general access
    fastapi_app.state.processing_service = default_processing_service
    fastapi_app.state.llm_client = (
        default_processing_service.llm_client
    )  # LLM client of default service
    fastapi_app.state.tools_provider = (
        default_processing_service.tools_provider
    )  # Tools provider of default service
    fastapi_app.state.tool_definitions = (
        await default_processing_service.tools_provider.get_tool_definitions()
    )
    logger.info(
        f"Default processing service set to profile ID: '{default_service_profile_id}'. Its components stored in app state."
    )

    # --- Instantiate Scraper ---
    # For now, PlaywrightScraper is instantiated directly.
    # Future: Could be made configurable (e.g. httpx scraper vs playwright)
    scraper_instance = PlaywrightScraper()  # Default user agent
    fastapi_app.state.scraper = scraper_instance  # Store scraper for DocumentIndexer
    logger.info(
        f"Scraper instance ({type(scraper_instance).__name__}) created and stored in app state."
    )

    # --- Instantiate Document Indexer ---
    # Load pipeline config from the main config dictionary
    pipeline_config_for_indexer = config.get("indexing_pipeline_config", {})
    if not pipeline_config_for_indexer.get("processors"):  # Basic validation
        logger.warning(
            "No processors defined in 'indexing_pipeline_config'. Document indexing might be limited."
        )

    # DocumentIndexer uses the LLM client from the default processing service for now
    # TODO: Consider if DocumentIndexer needs a specific LLM profile or if default is fine.
    document_indexer = DocumentIndexer(
        pipeline_config=pipeline_config_for_indexer,
        llm_client=default_processing_service.llm_client,
        embedding_generator=embedding_generator,  # Pass the main embedding generator
        scraper=scraper_instance,  # Pass the scraper instance
    )
    logger.info(
        "DocumentIndexer initialized using 'indexing_pipeline_config' from application configuration."
    )

    # --- Instantiate Email Indexer ---
    email_indexer = EmailIndexer(pipeline=document_indexer.pipeline)
    logger.info("EmailIndexer initialized with the main indexing pipeline.")

    # --- Instantiate Telegram Service ---
    telegram_service = TelegramService(
        telegram_token=config["telegram_token"],
        allowed_user_ids=config["allowed_user_ids"],
        developer_chat_id=config["developer_chat_id"],
        processing_service=default_processing_service,  # Default service for non-slash command interactions
        processing_services_registry=processing_services_registry,  # Pass the full registry
        app_config=config,  # Pass the main application config for slash command mapping
        get_db_context_func=get_db_context,
        new_task_event=new_task_event,  # Pass the global event
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

    # Task worker uses the default processing service and its config for callbacks etc.
    # TODO: LLM Callbacks might need to specify a profile_id if they are not meant for default.
    default_profile_for_worker_conf = None
    for prof in resolved_profiles:
        if prof["id"] == default_service_profile_id:
            default_profile_for_worker_conf = prof
            break
    if (
        not default_profile_for_worker_conf
    ):  # Should not happen if default_processing_service was found
        raise SystemExit(
            f"Could not find configuration for default profile ID '{default_service_profile_id}' for TaskWorker."
        )

    task_worker_instance = TaskWorker(
        processing_service=default_processing_service,
        chat_interface=telegram_service.chat_interface,
        new_task_event=new_task_event,  # Pass the global event
        calendar_config=default_profile_for_worker_conf["processing_config"][
            "calendar_config"
        ],
        timezone_str=default_profile_for_worker_conf["processing_config"]["timezone"],
        embedding_generator=embedding_generator,
    )

    # --- Register Task Handlers with the Worker Instance ---
    task_worker_instance.register_task_handler(
        "log_message",
        task_wrapper_handle_log_message,  # Use wrapper
    )
    # Register document processing handler from the indexer instance
    task_worker_instance.register_task_handler(
        "process_uploaded_document", document_indexer.process_document
    )
    # Register email indexing handler from the EmailIndexer instance
    task_worker_instance.register_task_handler(
        "index_email", email_indexer.handle_index_email
    )
    # Register LLM callback handler directly (dependencies passed via context by worker)
    task_worker_instance.register_task_handler("llm_callback", handle_llm_callback)

    # --- Register Indexing Task Handlers ---
    task_worker_instance.register_task_handler(
        "embed_and_store_batch", handle_embed_and_store_batch
    )
    logger.info(
        f"Registered task handlers for worker {task_worker_instance.worker_id}: {list(task_worker_instance.get_task_handlers().keys())}"
    )

    # Start the task queue worker using the instance's run method
    asyncio.create_task(
        task_worker_instance.run(new_task_event)  # Pass the event to the run method
    )

    # Wait until shutdown signal is received
    await shutdown_event.wait()

    logger.info("Shutdown signal received. Stopping services...")

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

    # Return the telegram service and the ToolsProvider of the default service
    return telegram_service, default_processing_service.tools_provider


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
    if args.document_storage_path is not None:
        config_data["document_storage_path"] = args.document_storage_path
        logger.info(
            f"Overriding document storage path with CLI argument: {args.document_storage_path}"
        )
    if args.attachment_storage_path is not None:
        config_data["attachment_storage_path"] = args.attachment_storage_path
        logger.info(
            f"Overriding attachment storage path with CLI argument: {args.attachment_storage_path}"
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

        # Corrected signal handler setup using both instances:
        for sig_num, sig_name in signal_map.items():
            # Pass service and generic tools provider instances (which might be None)
            loop.add_signal_handler(
                sig_num,
                lambda name=sig_name,
                service=telegram_service_instance,
                tools=tools_provider_instance: asyncio.create_task(
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

        logger.info("Application finished.")

    return 0  # Return 0 on successful shutdown


if __name__ == "__main__":
    sys.exit(main())  # Exit with the return code from main()
