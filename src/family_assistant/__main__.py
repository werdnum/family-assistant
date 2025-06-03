import argparse
import asyncio
import copy  # Keep for deep_merge_dicts
import json  # Keep for config logging and mcp_config.json
import logging
import os
import signal
import sys
import zoneinfo  # Keep for timezone validation in load_config
from typing import Any

import yaml  # Keep for config.yaml and prompts.yaml
from dotenv import load_dotenv  # Keep for .env loading

# Import the FastAPI app (needed for app.state)
from family_assistant.web.app_creator import app as fastapi_app

# Import the new Assistant class
from .assistant import (  # Import helpers too
    Assistant,
)

# --- Logging Configuration ---
# Set root logger level back to INFO
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# Keep external libraries less verbose
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(
    logging.INFO
)  # Or as configured by TelegramService
logging.getLogger("apscheduler").setLevel(logging.INFO)
logging.getLogger("caldav").setLevel(logging.INFO)
logger = logging.getLogger(__name__)


# --- Configuration Loading and Helper Functions ---
# These functions remain in __main__.py as they deal with environment interaction
# before the Assistant class is instantiated or are utility.
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
        "model": "gemini/gemini-2.5-pro-preview-05-06",
        "embedding_model": "gemini/gemini-embedding-exp-03-07",
        "embedding_dimensions": 1536,
        "database_url": "sqlite+aiosqlite:///family_assistant.db",
        "litellm_debug": False,
        "server_url": "http://localhost:8000",
        "document_storage_path": "/mnt/data/files",
        "attachment_storage_path": "/mnt/data/mailbox/attachments",
        "willyweather_api_key": None,  # Added for Weather Provider
        "willyweather_location_id": None,  # Added for Weather Provider
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
                "llm_model": "gemini/gemini-2.5-pro-preview-05-06",  # Use actual default model string
            },
            "chat_id_to_name_map": {},  # Added for known user mapping
            "tools_config": {
                # By default, if 'enable_local_tools' or 'enable_mcp_server_ids' are not specified
                # in a profile or here, all available tools of that type will be enabled.
                # An explicit empty list [] in a profile's config means NO tools of that type.
                "confirm_tools": [],  # Default empty; overridden by config.yaml or env var
                "mcp_initialization_timeout_seconds": 60,  # Default 1 minute
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
    config_data["willyweather_api_key"] = os.getenv(  # Load WillyWeather API Key
        "WILLYWEATHER_API_KEY", config_data.get("willyweather_api_key")
    )
    willyweather_loc_id_env = os.getenv("WILLYWEATHER_LOCATION_ID")
    if willyweather_loc_id_env:
        try:
            config_data["willyweather_location_id"] = int(willyweather_loc_id_env)
        except ValueError:
            logger.error(
                f"Invalid WILLYWEATHER_LOCATION_ID: '{willyweather_loc_id_env}'. Must be an integer. Using previous value: {config_data.get('willyweather_location_id')}"
            )
    # If not set or invalid, it remains None or its previous value from YAML/defaults.

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
    profile_tools_config = profile_settings[
        "tools_config"
    ]  # This is a reference to the dict

    # MCP Initialization Timeout from Env Var
    mcp_timeout_env = os.getenv("MCP_INITIALIZATION_TIMEOUT_SECONDS")
    if mcp_timeout_env is not None:
        try:
            profile_tools_config["mcp_initialization_timeout_seconds"] = int(
                mcp_timeout_env
            )
            logger.info(
                f"Loaded MCP initialization timeout from environment variable: {profile_tools_config['mcp_initialization_timeout_seconds']}s"
            )
        except ValueError:
            logger.error(
                f"Invalid MCP_INITIALIZATION_TIMEOUT_SECONDS: '{mcp_timeout_env}'. Must be an integer. Using previous value."
            )

    profile_proc_config["timezone"] = os.getenv(
        "TIMEZONE", profile_proc_config["timezone"]
    )

    # Load Home Assistant API URL and Token from environment variables for default profile settings
    profile_proc_config["home_assistant_api_url"] = os.getenv(
        "HOMEASSISTANT_URL", profile_proc_config.get("home_assistant_api_url")
    )
    profile_proc_config["home_assistant_token"] = os.getenv(
        "HOMEASSISTANT_API_KEY", profile_proc_config.get("home_assistant_token")
    )
    if profile_proc_config.get("home_assistant_api_url") or profile_proc_config.get(
        "home_assistant_token"
    ):
        logger.info(
            "Loaded Home Assistant API URL (using HOMEASSISTANT_URL) and Token (using HOMEASSISTANT_API_KEY) from environment variables into default profile settings."
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
            "willyweather_api_key",  # Exclude weather API key
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


def reload_config_handler(signum: int, frame: Any) -> None:
    """Handles SIGHUP for config reloading (placeholder)."""
    # Note: Frame type is `types.FrameType | None` but Any for simplicity here.
    logger.info(f"Received signal {signum}. Basic config reload triggered.")
    # This currently only re-calls load_config. A running Assistant instance
    # would not automatically pick up these changes without further logic.
    load_config()


def main() -> int:
    """Loads config, parses args, sets up event loop, and runs the application."""
    config_data = load_config()
    args = parser.parse_args()

    # Apply CLI Overrides to config_data
    if args.telegram_token is not None:
        config_data["telegram_token"] = args.telegram_token
    if args.openrouter_api_key is not None:
        config_data["openrouter_api_key"] = args.openrouter_api_key
    if args.model is not None:
        config_data["model"] = args.model
    if args.embedding_model is not None:
        config_data["embedding_model"] = args.embedding_model
    if args.embedding_dimensions is not None:
        config_data["embedding_dimensions"] = args.embedding_dimensions
    if args.document_storage_path is not None:
        config_data["document_storage_path"] = args.document_storage_path
    if args.attachment_storage_path is not None:
        config_data["attachment_storage_path"] = args.attachment_storage_path

    fastapi_app.state.config = config_data
    logger.info("Stored final configuration dictionary in FastAPI app state.")

    # LLM client overrides would be passed here if needed for main execution,
    # but typically this is for testing. For main run, it's None.
    assistant_app = Assistant(config_data, llm_client_overrides=None)
    loop = asyncio.get_event_loop()

    # Setup Signal Handlers
    signal_map = {signal.SIGINT: "SIGINT", signal.SIGTERM: "SIGTERM"}
    for sig_num, sig_name in signal_map.items():
        loop.add_signal_handler(
            sig_num,
            lambda name=sig_name,
            app_instance=assistant_app: app_instance.initiate_shutdown(name),
        )

    if hasattr(signal, "SIGHUP"):
        try:
            # reload_config_handler needs to be adapted if it re-initializes parts of assistant_app
            # For now, it just reloads config_data which isn't automatically re-read by a running Assistant instance.
            # A more robust reload would involve assistant_app.reload_config(new_config_data) or similar.
            loop.add_signal_handler(
                signal.SIGHUP, reload_config_handler, signal.SIGHUP, None
            )
            logger.info("SIGHUP handler registered for config reload (basic).")
        except NotImplementedError:
            logger.warning("SIGHUP signal handler not supported on this platform.")
        except Exception as e:
            logger.error(f"Failed to set SIGHUP handler: {e}")

    try:
        logger.info("Starting application via Assistant class...")
        loop.run_until_complete(assistant_app.setup_dependencies())
        loop.run_until_complete(
            assistant_app.start_services()
        )  # This will block until shutdown

        # After start_services() completes (meaning shutdown_event was set and initial stop began)
        # we ensure full stop_services logic runs.
        if not assistant_app.is_shutdown_complete():
            logger.info(
                "Ensuring all services are stopped post-start_services completion..."
            )
            loop.run_until_complete(assistant_app.stop_services())

    except ValueError as config_err:
        logger.critical(f"Configuration error during startup: {config_err}")
        return 1
    except (KeyboardInterrupt, SystemExit) as ex:
        logger.warning(
            f"Received {type(ex).__name__} in main, initiating shutdown sequence."
        )
        if not assistant_app.is_shutdown_complete():
            if not assistant_app.shutdown_event.is_set():
                assistant_app.initiate_shutdown(type(ex).__name__)
            # Wait for start_services to react to shutdown_event or call stop_services directly
            # This ensures that if start_services was interrupted before its own shutdown logic,
            # stop_services is still called.
            logger.info(f"Ensuring stop_services is called due to {type(ex).__name__}")
            loop.run_until_complete(assistant_app.stop_services())
    except Exception as e:
        logger.critical(f"Unhandled exception in main: {e}", exc_info=True)
        if not assistant_app.is_shutdown_complete():
            logger.error("Attempting emergency shutdown due to unhandled exception.")
            if not assistant_app.shutdown_event.is_set():
                assistant_app.initiate_shutdown(
                    f"UnhandledException: {type(e).__name__}"
                )
            loop.run_until_complete(assistant_app.stop_services())
        return 1
    finally:
        # Ensure event loop cleanup happens after all async operations
        # including those in stop_services.
        # Cancel any remaining tasks that might have been spawned outside of Assistant's control
        # or if stop_services was interrupted.
        remaining_tasks = [t for t in asyncio.all_tasks(loop=loop) if not t.done()]
        if remaining_tasks:
            logger.info(
                f"Cancelling {len(remaining_tasks)} remaining tasks in main finally block..."
            )
            for task in remaining_tasks:
                task.cancel()
            loop.run_until_complete(
                asyncio.gather(*remaining_tasks, return_exceptions=True)
            )
            logger.info("Remaining tasks cancelled.")

        logger.info("Closing event loop.")
        if not loop.is_closed():
            loop.close()

        logger.info("Application finished.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
