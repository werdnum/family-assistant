"""Configuration loading with clear priority hierarchy.

Configuration is loaded in the following priority order (lowest to highest):
1. Pydantic model defaults (defined in config_models.py)
2. config.yaml file
3. Environment variables
4. CLI arguments (applied after load_config returns)

This module provides a clean, testable implementation of configuration loading
with clear separation between different config sources.
"""

# ast-grep-ignore-block: no-dict-any - Config loading works with dynamic YAML/JSON data

from __future__ import annotations

import copy
import json
import logging
import os
import pathlib
import string
import zoneinfo
from dataclasses import dataclass
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import ValidationError

from .config_models import AppConfig

logger = logging.getLogger(__name__)

# Default paths
# defaults.yaml: Shipped with the application, contains default configuration
# config.yaml: Operator-provided, overrides defaults.yaml
DEFAULT_DEFAULTS_FILE = "defaults.yaml"
DEFAULT_CONFIG_FILE = "config.yaml"
DEFAULT_PROMPTS_FILE = "prompts.yaml"
DEFAULT_MCP_CONFIG_FILE = "mcp_config.json"


@dataclass
class EnvVarMapping:
    """Defines how an environment variable maps to a config path.

    Attributes:
        env_var: Environment variable name
        config_path: Dot-separated path in config dict (e.g., "pwa_config.vapid_public_key")
        value_type: Type to convert the value to (str, int, bool, list, dict)
        list_separator: Separator for list values (default ",")
        dict_separator: Separator for dict key:value pairs (default ":")
    """

    env_var: str
    config_path: str
    value_type: type = str
    list_separator: str = ","
    dict_separator: str = ":"


# Centralized environment variable mappings
# These define exactly which env vars are supported and where they map to
ENV_VAR_MAPPINGS: list[EnvVarMapping] = [
    # Secrets and API keys
    EnvVarMapping("TELEGRAM_BOT_TOKEN", "telegram_token"),
    EnvVarMapping("OPENROUTER_API_KEY", "openrouter_api_key"),
    EnvVarMapping("GEMINI_API_KEY", "gemini_api_key"),
    EnvVarMapping("WILLYWEATHER_API_KEY", "willyweather_api_key"),
    EnvVarMapping("WILLYWEATHER_LOCATION_ID", "willyweather_location_id", int),
    # Database and server
    EnvVarMapping("DATABASE_URL", "database_url"),
    EnvVarMapping("SERVER_URL", "server_url"),
    EnvVarMapping("DOCUMENT_STORAGE_PATH", "document_storage_path"),
    EnvVarMapping("ATTACHMENT_STORAGE_PATH", "attachment_storage_path"),
    EnvVarMapping("CHAT_ATTACHMENT_STORAGE_PATH", "chat_attachment_storage_path"),
    # Model configuration
    EnvVarMapping("LLM_MODEL", "model"),
    EnvVarMapping("EMBEDDING_MODEL", "embedding_model"),
    EnvVarMapping("EMBEDDING_DIMENSIONS", "embedding_dimensions", int),
    # Debug flags
    EnvVarMapping("LITELLM_DEBUG", "litellm_debug", bool),
    EnvVarMapping("DEBUG_LLM_MESSAGES", "debug_llm_messages", bool),
    # PWA configuration
    EnvVarMapping("VAPID_PUBLIC_KEY", "pwa_config.vapid_public_key"),
    EnvVarMapping("VAPID_PRIVATE_KEY", "pwa_config.vapid_private_key"),
    EnvVarMapping("VAPID_CONTACT_EMAIL", "pwa_config.vapid_contact_email"),
    # Profile settings
    EnvVarMapping("DEFAULT_SERVICE_PROFILE_ID", "default_service_profile_id"),
    EnvVarMapping("TIMEZONE", "default_profile_settings.processing_config.timezone"),
    EnvVarMapping(
        "HOMEASSISTANT_URL",
        "default_profile_settings.processing_config.home_assistant_api_url",
    ),
    EnvVarMapping(
        "HOMEASSISTANT_API_KEY",
        "default_profile_settings.processing_config.home_assistant_token",
    ),
    EnvVarMapping(
        "MCP_INITIALIZATION_TIMEOUT_SECONDS",
        "default_profile_settings.tools_config.mcp_initialization_timeout_seconds",
        int,
    ),
    # User access control (list types)
    EnvVarMapping("ALLOWED_USER_IDS", "allowed_user_ids", list),
    EnvVarMapping("DEVELOPER_CHAT_ID", "developer_chat_id", int),
    # Tools configuration
    EnvVarMapping(
        "TOOLS_REQUIRING_CONFIRMATION",
        "default_profile_settings.tools_config.confirm_tools",
        list,
    ),
    # Chat ID to name map (dict type)
    EnvVarMapping(
        "CHAT_ID_TO_NAME_MAP",
        "default_profile_settings.chat_id_to_name_map",
        dict,
    ),
]


def deep_merge_dicts(
    base_dict: dict[str, Any],
    merge_dict: dict[str, Any],  # noqa: ANN401
) -> dict[str, Any]:  # noqa: ANN401
    """Deeply merges merge_dict into base_dict.

    Args:
        base_dict: The base dictionary to merge into
        merge_dict: The dictionary to merge from (values take precedence)

    Returns:
        A new dictionary with deeply merged values
    """
    result = copy.deepcopy(base_dict)
    for key, value in merge_dict.items():
        if isinstance(value, dict) and key in result and isinstance(result[key], dict):
            result[key] = deep_merge_dicts(result[key], value)
        else:
            result[key] = copy.deepcopy(value) if isinstance(value, dict) else value
    return result


def set_nested_value(
    data: dict[str, Any],  # noqa: ANN401
    path: str,
    value: Any,  # noqa: ANN401
) -> None:
    """Set a value at a nested path in a dictionary.

    Args:
        data: The dictionary to modify
        path: Dot-separated path (e.g., "pwa_config.vapid_public_key")
        value: The value to set
    """
    keys = path.split(".")
    current = data
    for key in keys[:-1]:
        if key not in current:
            current[key] = {}
        current = current[key]
    current[keys[-1]] = value


def get_nested_value(
    data: dict[str, Any],  # noqa: ANN401
    path: str,
    default: Any = None,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    """Get a value at a nested path in a dictionary.

    Args:
        data: The dictionary to read from
        path: Dot-separated path (e.g., "pwa_config.vapid_public_key")
        default: Default value if path doesn't exist

    Returns:
        The value at the path, or default if not found
    """
    keys = path.split(".")
    current = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def parse_env_value(
    value: str,
    value_type: type,
    list_separator: str = ",",
    dict_separator: str = ":",
) -> Any:  # noqa: ANN401
    """Parse an environment variable value to the specified type.

    Args:
        value: The raw string value from the environment
        value_type: The target type (str, int, bool, list, dict)
        list_separator: Separator for list values
        dict_separator: Separator for dict key:value pairs

    Returns:
        The parsed value in the target type

    Raises:
        ValueError: If the value cannot be converted to the target type
    """
    if value_type is str:
        return value

    if value_type is int:
        return int(value)

    if value_type is bool:
        return value.lower() in {"true", "1", "yes"}

    if value_type is list:
        # Parse as list of integers if all values are numeric
        items = [item.strip() for item in value.split(list_separator) if item.strip()]
        try:
            return [int(item) for item in items]
        except ValueError:
            return items

    if value_type is dict:
        # Parse as dict with int keys (for chat_id_to_name_map format: "123:Alice,456:Bob")
        result: dict[int, str] = {}
        pairs = value.split(list_separator)
        for pair in pairs:
            if dict_separator in pair:
                key_str, val = pair.split(dict_separator, 1)
                result[int(key_str.strip())] = val.strip()
        return result

    return value


def load_yaml_file(
    file_path: str | pathlib.Path,
) -> dict[str, Any]:  # noqa: ANN401
    """Load a YAML file, returning an empty dict if not found.

    Args:
        file_path: Path to the YAML file

    Returns:
        The loaded YAML content as a dictionary, or empty dict if file not found
    """
    try:
        with open(file_path, encoding="utf-8") as f:
            content = yaml.safe_load(f)
            if isinstance(content, dict):
                return content
            logger.warning(f"{file_path} is not a valid dictionary. Ignoring.")
            return {}
    except FileNotFoundError:
        logger.info(f"{file_path} not found. Using defaults.")
        return {}
    except yaml.YAMLError as e:
        logger.error(f"Error parsing {file_path}: {e}. Using defaults.")
        return {}


def load_json_file(
    file_path: str | pathlib.Path,
) -> dict[str, Any]:  # noqa: ANN401
    """Load a JSON file, returning an empty dict if not found.

    Args:
        file_path: Path to the JSON file

    Returns:
        The loaded JSON content as a dictionary, or empty dict if file not found
    """
    try:
        with open(file_path, encoding="utf-8") as f:
            content = json.load(f)
            if isinstance(content, dict):
                return content
            logger.warning(f"{file_path} is not a valid dictionary. Ignoring.")
            return {}
    except FileNotFoundError:
        logger.info(f"{file_path} not found.")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing {file_path}: {e}.")
        return {}


def expand_env_vars_in_dict(
    data: Any,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    """Recursively expand environment variables in dictionary values.

    Uses ${VAR} syntax for safer expansion to avoid accidental substitution
    of literal dollar signs in configuration values.

    Args:
        data: The data structure to process (dict, list, or scalar)

    Returns:
        The data with environment variables expanded
    """
    if isinstance(data, dict):
        return {key: expand_env_vars_in_dict(value) for key, value in data.items()}
    elif isinstance(data, list):
        return [expand_env_vars_in_dict(item) for item in data]
    elif isinstance(data, str):
        template = string.Template(data)
        try:
            return template.substitute(os.environ)
        except (KeyError, ValueError) as e:
            logger.warning(
                f"Environment variable expansion failed: {e}. Using original value: {data}"
            )
            return data
    else:
        return data


def get_code_defaults() -> dict[str, Any]:  # noqa: ANN401
    """Get the default configuration values as a dictionary.

    These defaults match the existing behavior in __main__.py but are now
    centralized and more maintainable. Many defaults are also defined in
    the Pydantic models in config_models.py.

    Returns:
        Dictionary with all default configuration values
    """
    return {
        "telegram_token": None,
        "telegram_enabled": True,
        "openrouter_api_key": None,
        "gemini_api_key": None,
        "allowed_user_ids": [],
        "developer_chat_id": None,
        "model": "gemini/gemini-2.5-pro",
        "embedding_model": "gemini/gemini-embedding-001",
        "embedding_dimensions": 1536,
        "database_url": "sqlite+aiosqlite:///family_assistant.db",
        "litellm_debug": False,
        "debug_llm_messages": False,
        "server_url": "http://localhost:8000",
        "document_storage_path": "/mnt/data/files",
        "attachment_storage_path": "/mnt/data/mailbox/attachments",
        "chat_attachment_storage_path": "/tmp/chat_attachments",
        "willyweather_api_key": None,
        "willyweather_location_id": None,
        "calendar_config": {
            "duplicate_detection": {
                "enabled": True,
                "similarity_strategy": "embedding",
                "similarity_threshold": 0.30,
                "time_window_hours": 2,
                "embedding": {
                    "model": "sentence-transformers/all-MiniLM-L6-v2",
                    "device": "cpu",
                },
            }
        },
        "pwa_config": {
            "vapid_public_key": None,
            "vapid_private_key": None,
            "vapid_contact_email": None,
        },
        "llm_parameters": {},
        "gemini_live_config": {
            "model": "gemini-2.5-flash-native-audio-preview-09-2025",
            "voice": {"name": "Puck"},
            "session": {"max_duration_minutes": 15},
            "transcription": {"input_enabled": True, "output_enabled": True},
            "vad": {
                "automatic": True,
                "start_of_speech_sensitivity": "DEFAULT",
                "end_of_speech_sensitivity": "DEFAULT",
                "prefix_padding_ms": None,
                "silence_duration_ms": None,
            },
            "affective_dialog": {"enabled": False},
            "proactivity": {"enabled": False, "proactive_audio": False},
            "thinking": {"include_thoughts": False},
        },
        "mcp_config": {"mcpServers": {}},
        "default_service_profile_id": "default_assistant",
        "service_profiles": [],
        "indexing_pipeline_config": {
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
                            "fetched_content_markdown",
                        ]
                    },
                },
            ]
        },
        "default_profile_settings": {
            "processing_config": {
                "prompts": {},
                "timezone": "UTC",
                "max_history_messages": 5,
                "history_max_age_hours": 24,
                "web_max_history_messages": 100,
                "web_history_max_age_hours": 720,
                "llm_model": "gemini/gemini-2.5-pro",
            },
            "chat_id_to_name_map": {},
            "tools_config": {
                "confirm_tools": [],
                "mcp_initialization_timeout_seconds": 60,
            },
            "slash_commands": [],
        },
    }


def apply_env_var_overrides(
    config_data: dict[str, Any],  # noqa: ANN401
    mappings: list[EnvVarMapping] | None = None,
) -> None:
    """Apply environment variable overrides to configuration.

    Args:
        config_data: The configuration dictionary to modify in place
        mappings: List of env var mappings to apply (defaults to ENV_VAR_MAPPINGS)
    """
    if mappings is None:
        mappings = ENV_VAR_MAPPINGS

    for mapping in mappings:
        env_value = os.getenv(mapping.env_var)  # pylint: disable=invalid-envvar-value
        if env_value is not None:
            try:
                parsed_value = parse_env_value(
                    env_value,
                    mapping.value_type,
                    mapping.list_separator,
                    mapping.dict_separator,
                )
                set_nested_value(config_data, mapping.config_path, parsed_value)
                logger.debug(
                    f"Applied env var {mapping.env_var} to {mapping.config_path}"
                )
            except ValueError as e:
                logger.error(
                    f"Invalid value for {mapping.env_var}: {e}. Using previous value."
                )

    # Handle ALLOWED_CHAT_IDS as alias for ALLOWED_USER_IDS
    allowed_chat_ids = os.getenv("ALLOWED_CHAT_IDS")
    if allowed_chat_ids is not None and os.getenv("ALLOWED_USER_IDS") is None:
        try:
            ids = [
                int(cid.strip()) for cid in allowed_chat_ids.split(",") if cid.strip()
            ]
            config_data["allowed_user_ids"] = ids
        except ValueError:
            logger.error("Invalid ALLOWED_CHAT_IDS format. Using previous value.")


def apply_calendar_env_vars(
    config_data: dict[str, Any],  # noqa: ANN401
) -> None:
    """Apply calendar-specific environment variable overrides.

    These are handled separately because they have complex merging behavior
    (preserving duplicate_detection settings while overriding caldav/ical).

    Args:
        config_data: The configuration dictionary to modify in place
    """
    caldav_user = os.getenv("CALDAV_USERNAME")
    caldav_pass = os.getenv("CALDAV_PASSWORD")
    caldav_urls_str = os.getenv("CALDAV_CALENDAR_URLS")
    ical_urls_str = os.getenv("ICAL_URLS")

    temp_calendar_config: dict[str, Any] = {}

    if caldav_user and caldav_pass and caldav_urls_str:
        caldav_urls = [url.strip() for url in caldav_urls_str.split(",") if url.strip()]
        if caldav_urls:
            temp_calendar_config["caldav"] = {
                "username": caldav_user,
                "password": caldav_pass,
                "calendar_urls": caldav_urls,
            }
            logger.info("Loaded CalDAV config from environment variables.")

    if ical_urls_str:
        ical_urls = [url.strip() for url in ical_urls_str.split(",") if url.strip()]
        if ical_urls:
            temp_calendar_config["ical"] = {"urls": ical_urls}
            logger.info("Loaded iCal config from environment variables.")

    if temp_calendar_config:
        # Preserve duplicate_detection settings from existing config
        existing_dup_detection = config_data.get("calendar_config", {}).get(
            "duplicate_detection", {}
        )
        config_data["calendar_config"] = temp_calendar_config
        if existing_dup_detection:
            config_data["calendar_config"]["duplicate_detection"] = (
                existing_dup_detection
            )


def validate_timezone(
    config_data: dict[str, Any],  # noqa: ANN401
) -> None:
    """Validate and fix timezone in profile settings.

    Args:
        config_data: The configuration dictionary to check/modify
    """
    profile_settings = config_data.get("default_profile_settings", {})
    proc_config = profile_settings.get("processing_config", {})
    timezone = proc_config.get("timezone", "UTC")

    try:
        zoneinfo.ZoneInfo(timezone)
    except zoneinfo.ZoneInfoNotFoundError:
        logger.error(f"Invalid timezone '{timezone}'. Defaulting to UTC.")
        proc_config["timezone"] = "UTC"


def merge_yaml_config(
    base_config: dict[str, Any],  # noqa: ANN401
    yaml_config: dict[str, Any],  # noqa: ANN401
) -> None:
    """Merge YAML config into base config with proper handling of nested structures.

    Args:
        base_config: The base configuration to merge into (modified in place)
        yaml_config: The YAML configuration to merge from
    """
    for key, value in yaml_config.items():
        if key == "default_profile_settings" and isinstance(value, dict):
            base_dps = base_config.setdefault("default_profile_settings", {})
            # Deep merge for processing_config
            if "processing_config" in value:
                base_dps.setdefault("processing_config", {}).update(
                    value["processing_config"]
                )
            # Deep merge for tools_config
            if "tools_config" in value:
                base_dps.setdefault("tools_config", {}).update(value["tools_config"])
            # Deep merge for chat_id_to_name_map
            if "chat_id_to_name_map" in value:
                base_dps.setdefault("chat_id_to_name_map", {}).update(
                    value["chat_id_to_name_map"]
                )
            # Handle slash_commands (replace, not merge)
            if "slash_commands" in value:
                base_dps["slash_commands"] = value["slash_commands"]
            # Handle other keys
            for sub_key in value:
                if sub_key not in {
                    "processing_config",
                    "tools_config",
                    "chat_id_to_name_map",
                    "slash_commands",
                }:
                    base_dps[sub_key] = value[sub_key]
        elif (
            key in base_config
            and isinstance(value, dict)
            and isinstance(base_config[key], dict)
        ):
            base_config[key].update(value)
        else:
            base_config[key] = value


def load_prompts_yaml(
    prompts_file_path: str = DEFAULT_PROMPTS_FILE,
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:  # noqa: ANN401
    """Load prompts from YAML file.

    Args:
        prompts_file_path: Path to the prompts YAML file

    Returns:
        Tuple of (default_prompts, service_profiles_prompts)
    """
    try:
        with open(prompts_file_path, encoding="utf-8") as f:
            loaded_prompts = yaml.safe_load(f)
            if isinstance(loaded_prompts, dict):
                service_profiles = loaded_prompts.pop("service_profiles", {})
                if service_profiles:
                    logger.info(
                        f"Found {len(service_profiles)} profile-specific prompt overrides in {prompts_file_path}"
                    )
                logger.info(f"Successfully loaded prompts from {prompts_file_path}")
                return loaded_prompts, service_profiles
            else:
                logger.error(f"{prompts_file_path} is not a valid dictionary.")
                return {}, {}
    except FileNotFoundError:
        logger.warning(f"{prompts_file_path} not found. Using default prompts.")
        return {}, {}
    except yaml.YAMLError as e:
        logger.error(f"Error parsing {prompts_file_path}: {e}")
        return {}, {}


def load_user_documentation(filenames: list[str]) -> str:
    """Load user documentation files from docs/user/ directory.

    Args:
        filenames: List of filenames to load (e.g., ['USER_GUIDE.md', 'scripting.md'])

    Returns:
        Combined content from all files, separated by markdown headers.
        Returns empty string if no files can be loaded.
    """
    # Determine the docs/user directory
    docs_user_dir_env = os.getenv("DOCS_USER_DIR")
    if docs_user_dir_env:
        docs_user_dir = pathlib.Path(docs_user_dir_env).resolve()
    else:
        docs_user_dir = pathlib.Path("docs") / "user"
        # Try Docker default if the calculated path doesn't exist
        if not docs_user_dir.exists() and pathlib.Path("/app/docs/user").exists():
            docs_user_dir = pathlib.Path("/app/docs/user")

    if not docs_user_dir.is_dir():
        logger.warning(
            f"User documentation directory not found: '{docs_user_dir}'. "
            "Cannot load system docs for profile."
        )
        return ""

    allowed_extensions = {".md", ".txt"}
    combined_content = []

    for filename in filenames:
        # Security check: prevent directory traversal
        if ".." in filename or not any(
            filename.endswith(ext) for ext in allowed_extensions
        ):
            logger.warning(
                f"Skipping invalid documentation filename: '{filename}' "
                "(contains '..' or invalid extension)"
            )
            continue

        file_path = (docs_user_dir / filename).resolve()

        # Ensure resolved path is still within docs directory
        if docs_user_dir.resolve() not in file_path.parents:
            logger.warning(
                f"Skipping documentation file outside allowed directory: '{filename}'"
            )
            continue

        # Try to read the file
        try:
            with open(file_path, encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    header = f"\n\n# Included Documentation: {filename}\n\n"
                    combined_content.append(header + content)
                    logger.info(
                        f"Loaded user documentation: '{filename}' ({len(content)} chars)"
                    )
                else:
                    logger.warning(f"Documentation file is empty: '{filename}'")
        except FileNotFoundError:
            logger.warning(
                f"Documentation file not found: '{filename}' in '{docs_user_dir}'"
            )
        except Exception as e:
            logger.error(
                f"Error reading documentation file '{filename}': {e}", exc_info=True
            )

    return "\n".join(combined_content)


def resolve_service_profile(
    profile_def: dict[str, Any],  # noqa: ANN401
    default_settings: dict[str, Any],  # noqa: ANN401
    prompts_yaml_service_profiles: dict[str, dict[str, Any]],  # noqa: ANN401
) -> dict[str, Any]:  # noqa: ANN401
    """Resolve a single service profile by merging defaults and overrides.

    The merge priority is:
    1. default_profile_settings (base)
    2. prompts.yaml service_profiles (profile-specific prompts)
    3. config.yaml profile definition (takes precedence)

    Args:
        profile_def: The profile definition from config.yaml
        default_settings: The default_profile_settings to start from
        prompts_yaml_service_profiles: Profile-specific prompts from prompts.yaml

    Returns:
        The fully resolved profile configuration
    """
    resolved = copy.deepcopy(default_settings)
    resolved["id"] = profile_def["id"]
    resolved["description"] = profile_def.get("description", "")

    profile_id = profile_def["id"]

    # Merge profile-specific prompts from prompts.yaml (before config.yaml)
    if profile_id in prompts_yaml_service_profiles:
        prompts_override = prompts_yaml_service_profiles[profile_id]
        if isinstance(prompts_override, dict):
            resolved["processing_config"]["prompts"] = deep_merge_dicts(
                resolved["processing_config"].get("prompts", {}),
                prompts_override,
            )
            logger.info(f"Merged prompts from prompts.yaml for profile '{profile_id}'")

    # Merge processing_config from config.yaml
    if "processing_config" in profile_def and isinstance(
        profile_def["processing_config"], dict
    ):
        # Deep merge for 'prompts'
        if "prompts" in profile_def["processing_config"]:
            resolved["processing_config"]["prompts"] = deep_merge_dicts(
                resolved["processing_config"].get("prompts", {}),
                profile_def["processing_config"]["prompts"],
            )

        # Replace scalar values
        scalar_keys = [
            "provider",
            "llm_model",
            "timezone",
            "max_history_messages",
            "history_max_age_hours",
            "web_max_history_messages",
            "web_history_max_age_hours",
            "max_iterations",
            "delegation_security_level",
            "retry_config",
            "camera_config",
        ]
        for key in scalar_keys:
            if key in profile_def["processing_config"]:
                resolved["processing_config"][key] = profile_def["processing_config"][
                    key
                ]

        # Handle include_system_docs
        if "include_system_docs" in profile_def["processing_config"]:
            include_docs = profile_def["processing_config"]["include_system_docs"]
            if isinstance(include_docs, list) and include_docs:
                logger.info(
                    f"Profile '{profile_id}' configured to load system docs: {include_docs}"
                )
                loaded_content = load_user_documentation(include_docs)
                if loaded_content:
                    current_prompt = resolved["processing_config"]["prompts"].get(
                        "system_prompt", ""
                    )
                    if current_prompt:
                        resolved["processing_config"]["prompts"]["system_prompt"] = (
                            current_prompt + "\n" + loaded_content
                        )
                    else:
                        resolved["processing_config"]["prompts"]["system_prompt"] = (
                            loaded_content
                        )
                    logger.info(
                        f"Appended {len(loaded_content)} chars of docs to system prompt for '{profile_id}'"
                    )

    # Replace tools_config entirely if defined
    if "tools_config" in profile_def and isinstance(profile_def["tools_config"], dict):
        resolved["tools_config"] = profile_def["tools_config"]

    # Merge chat_id_to_name_map
    if "chat_id_to_name_map" in profile_def and isinstance(
        profile_def["chat_id_to_name_map"], dict
    ):
        resolved["chat_id_to_name_map"] = deep_merge_dicts(
            resolved.get("chat_id_to_name_map", {}),
            profile_def["chat_id_to_name_map"],
        )

    # Handle slash_commands (replace if present)
    if "slash_commands" in profile_def and isinstance(
        profile_def["slash_commands"], list
    ):
        resolved["slash_commands"] = profile_def["slash_commands"]

    return resolved


def resolve_all_service_profiles(
    config_data: dict[str, Any],  # noqa: ANN401
    prompts_yaml_service_profiles: dict[str, dict[str, Any]],  # noqa: ANN401
) -> list[dict[str, Any]]:  # noqa: ANN401
    """Resolve all service profiles from configuration.

    Args:
        config_data: The main configuration dictionary
        prompts_yaml_service_profiles: Profile-specific prompts from prompts.yaml

    Returns:
        List of fully resolved service profile configurations
    """
    yaml_profiles = config_data.get("service_profiles", [])
    if not isinstance(yaml_profiles, list):
        logger.warning("service_profiles is not a list. Ignoring.")
        yaml_profiles = []

    default_settings = config_data["default_profile_settings"]
    resolved_profiles = []

    for profile_def in yaml_profiles:
        if not isinstance(profile_def, dict) or "id" not in profile_def:
            logger.warning(f"Invalid profile definition (missing ID): {profile_def}")
            continue

        resolved = resolve_service_profile(
            profile_def, default_settings, prompts_yaml_service_profiles
        )
        resolved_profiles.append(resolved)

    # Create default profile if none defined
    if not resolved_profiles:
        logger.info("No service profiles defined. Creating default profile.")
        default_profile = copy.deepcopy(default_settings)
        default_profile["id"] = config_data["default_service_profile_id"]
        default_profile["description"] = "Default assistant profile."
        if "llm_model" not in default_profile["processing_config"]:
            default_profile["processing_config"]["llm_model"] = config_data["model"]
        resolved_profiles.append(default_profile)

    logger.info(
        f"Resolved {len(resolved_profiles)} service profiles. "
        f"Default ID: {config_data['default_service_profile_id']}"
    )

    return resolved_profiles


def load_mcp_config(
    mcp_config_path: str | None = None,
) -> dict[str, Any]:  # noqa: ANN401
    """Load MCP configuration from JSON file with environment variable expansion.

    Args:
        mcp_config_path: Path to MCP config file (uses MCP_CONFIG_PATH env or default)

    Returns:
        The MCP configuration dictionary with env vars expanded
    """
    if mcp_config_path is None:
        mcp_config_path = os.getenv("MCP_CONFIG_PATH", DEFAULT_MCP_CONFIG_FILE)

    mcp_config = load_json_file(mcp_config_path)
    if mcp_config:
        mcp_config = expand_env_vars_in_dict(mcp_config)
        logger.info(f"Loaded MCP config from {mcp_config_path}")
    return mcp_config


def load_indexing_pipeline_config(
    config_data: dict[str, Any],  # noqa: ANN401
) -> None:
    """Load indexing pipeline config from environment variable if set.

    Args:
        config_data: The configuration dictionary to modify in place
    """
    env_config = os.getenv("INDEXING_PIPELINE_CONFIG_JSON")
    if env_config:
        try:
            parsed = json.loads(env_config)
            if isinstance(parsed, dict):
                config_data["indexing_pipeline_config"] = parsed
                logger.info(
                    "Loaded indexing_pipeline_config from environment variable."
                )
            else:
                logger.warning(
                    "INDEXING_PIPELINE_CONFIG_JSON is not a valid dictionary."
                )
        except json.JSONDecodeError as e:
            logger.error(f"Error parsing INDEXING_PIPELINE_CONFIG_JSON: {e}")


def load_config(
    defaults_file_path: str = DEFAULT_DEFAULTS_FILE,
    config_file_path: str = DEFAULT_CONFIG_FILE,
    prompts_file_path: str = DEFAULT_PROMPTS_FILE,
    load_dotenv_file: bool = True,
) -> AppConfig:
    """Load configuration with clear priority hierarchy.

    Priority (lowest to highest):
    1. Code defaults (from get_code_defaults())
    2. defaults.yaml file (shipped with application)
    3. config.yaml file (operator-provided, optional)
    4. Environment variables

    CLI arguments should be applied after this function returns using
    AppConfig.model_copy(update={...}).

    Args:
        defaults_file_path: Path to the defaults YAML file (shipped with app)
        config_file_path: Path to the operator config YAML file (optional)
        prompts_file_path: Path to the prompts YAML file
        load_dotenv_file: Whether to load .env file (default True)

    Returns:
        A validated AppConfig model containing all configuration

    Raises:
        ValidationError: If configuration contains invalid keys or values
    """
    # 1. Start with code defaults
    config_data = get_code_defaults()
    logger.info("Initialized config with code defaults.")

    # 2. Load and merge defaults.yaml (shipped with application)
    defaults_yaml = load_yaml_file(defaults_file_path)
    if defaults_yaml:
        merge_yaml_config(config_data, defaults_yaml)
        logger.info(f"Merged defaults from {defaults_file_path}")

    # 3. Load and merge config.yaml (operator-provided, optional)
    user_config = load_yaml_file(config_file_path)
    if user_config:
        merge_yaml_config(config_data, user_config)
        logger.info(f"Merged operator configuration from {config_file_path}")

    # 4. Load environment variables from .env file
    if load_dotenv_file:
        load_dotenv()

    # 5. Apply environment variable overrides
    apply_env_var_overrides(config_data)
    apply_calendar_env_vars(config_data)

    # Handle telegram_enabled based on token presence
    if config_data.get("telegram_token"):
        config_data["telegram_enabled"] = True
    else:
        config_data["telegram_enabled"] = False

    # Validate timezone
    validate_timezone(config_data)

    # 6. Load prompts and resolve service profiles
    default_prompts, service_profile_prompts = load_prompts_yaml(prompts_file_path)
    if default_prompts:
        config_data["default_profile_settings"]["processing_config"]["prompts"] = (
            default_prompts
        )

    # 7. Load MCP config
    mcp_config = load_mcp_config()
    if mcp_config:
        config_data["mcp_config"] = mcp_config

    # 8. Load indexing pipeline config from env if present
    load_indexing_pipeline_config(config_data)

    # 9. Resolve service profiles
    config_data["service_profiles"] = resolve_all_service_profiles(
        config_data, service_profile_prompts
    )

    # 10. Log final config (excluding secrets)
    _log_config(config_data)

    # 11. Validate through Pydantic model
    try:
        validated_config = AppConfig.model_validate(config_data)
        logger.info("Configuration validated successfully through Pydantic model.")
        return validated_config
    except ValidationError as e:
        logger.error(f"Configuration validation failed: {e}")
        raise


def _log_config(
    config_data: dict[str, Any],  # noqa: ANN401
) -> None:
    """Log configuration excluding sensitive values.

    Args:
        config_data: The configuration dictionary to log
    """
    # Keys to exclude from logging
    secret_keys = {
        "telegram_token",
        "openrouter_api_key",
        "gemini_api_key",
        "willyweather_api_key",
        "database_url",
    }

    loggable = copy.deepcopy({
        k: v for k, v in config_data.items() if k not in secret_keys
    })

    # Remove password from calendar_config
    if "calendar_config" in loggable and "caldav" in loggable["calendar_config"]:
        loggable["calendar_config"]["caldav"].pop("password", None)

    # Remove VAPID private key
    if "pwa_config" in loggable:
        loggable["pwa_config"] = {
            k: v for k, v in loggable["pwa_config"].items() if k != "vapid_private_key"
        }

    logger.info(
        f"Final configuration (excluding secrets): {json.dumps(loggable, indent=2, default=str)}"
    )
