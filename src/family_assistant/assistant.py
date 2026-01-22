from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import os
import sys
import zoneinfo
from asyncio import subprocess as asyncio_subprocess
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import httpx
import uvicorn

# Import Embedding interface/clients
from family_assistant import embeddings
from family_assistant.config_models import AppConfig  # noqa: TC001  # Used at runtime
from family_assistant.config_models import (  # noqa: TC001  # Used at runtime
    CalendarConfig as PydanticCalendarConfig,
)

# Import the whole storage module for task queue functions etc.
# --- NEW: Import ContextProvider and its implementations ---
from family_assistant.context_providers import (
    CalendarContextProvider,
    HomeAssistantContextProvider,  # Added
    KnownUsersContextProvider,
    NotesContextProvider,
    WeatherContextProvider,
)
from family_assistant.embeddings import (
    EmbeddingGenerator,
    LiteLLMEmbeddingGenerator,
)
from family_assistant.events.home_assistant_source import HomeAssistantSource
from family_assistant.events.indexing_source import IndexingSource
from family_assistant.events.processor import EventProcessor
from family_assistant.home_assistant_shared import create_home_assistant_client
from family_assistant.indexing.document_indexer import DocumentIndexer
from family_assistant.indexing.email_indexer import EmailIndexer
from family_assistant.indexing.notes_indexer import NotesIndexer
from family_assistant.indexing.tasks import handle_embed_and_store_batch
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.services.push_notification import PushNotificationService
from family_assistant.storage import init_db
from family_assistant.storage.base import create_engine_with_sqlite_optimizations
from family_assistant.storage.context import (
    DatabaseContext,
    get_db_context,
)
from family_assistant.task_worker import (
    TaskWorker,
    handle_llm_callback,
    handle_reindex_document,
    handle_script_execution,
    handle_system_error_log_cleanup,
    handle_system_event_cleanup,
)
from family_assistant.task_worker import (
    handle_log_message as original_handle_log_message,
)
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS as local_tool_implementations,
)
from family_assistant.tools import (
    TOOLS_DEFINITION as local_tools_definition,
)
from family_assistant.tools import (
    CompositeToolsProvider,
    ConfirmingToolsProvider,
    FilteredToolsProvider,
    LocalToolsProvider,
    MCPToolsProvider,
    _scan_user_docs,
)
from family_assistant.utils.logging_handler import setup_error_logging
from family_assistant.utils.scraping import PlaywrightScraper
from family_assistant.web.app_creator import configure_app_auth, create_app
from family_assistant.web.message_notifier import MessageNotifier

from .telegram.service import TelegramService

if TYPE_CHECKING:
    import socket

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.llm import LLMInterface
    from family_assistant.services.attachment_registry import AttachmentRegistry
    from family_assistant.tools.types import CalendarConfig as CalendarConfigDict
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


def _calendar_config_to_dict(
    pydantic_config: PydanticCalendarConfig,
) -> CalendarConfigDict:
    """Convert Pydantic CalendarConfig to TypedDict format for tool functions."""
    return cast("CalendarConfigDict", pydantic_config.model_dump(exclude_none=True))


# Helper function (can be moved to utils if used elsewhere)
def deep_merge_dicts(base_dict: dict, merge_dict: dict) -> dict:
    """Deeply merges merge_dict into base_dict."""
    result = copy.deepcopy(base_dict)
    for key, value in merge_dict.items():
        if isinstance(value, dict) and key in result and isinstance(result[key], dict):
            result[key] = deep_merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


class NullChatInterface:
    """A null chat interface for when Telegram service is not configured."""

    async def send_message(
        self,
        conversation_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_to_interface_id: str | None = None,
        attachment_ids: list[str] | None = None,
    ) -> str | None:
        """Does nothing, returns None."""
        logger.debug(
            "NullChatInterface: send_message called for conversation %s: %s",
            conversation_id,
            text,
        )
        return None


# --- Wrapper Functions for Type Compatibility ---
# These wrappers might be needed by task handlers if they are registered from here
# or if the Assistant class sets up the task worker directly.


async def task_wrapper_handle_log_message(
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    exec_context: ToolExecutionContext,
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    payload: dict[str, Any],
) -> None:
    """
    Wrapper for the original handle_log_message to match TaskWorker's expected handler signature.
    It extracts db_context from ToolExecutionContext and ensures payload is a dict.
    """
    if not isinstance(payload, dict):
        logger.error(
            f"Payload for handle_log_message task is not a dict: {type(payload)}. Content: {payload}"
        )
        return
    await original_handle_log_message(exec_context.db_context, payload)


class Assistant:
    """
    Orchestrates the Family Assistant application's lifecycle, including
    dependency setup, service initialization, and graceful shutdown.
    """

    def __init__(
        self,
        config: AppConfig,
        llm_client_overrides: dict[str, LLMInterface] | None = None,
        database_engine: AsyncEngine | None = None,
        server_socket: socket.socket | None = None,
    ) -> None:
        self.config: AppConfig = config
        self._injected_database_engine = database_engine
        self.shutdown_event = asyncio.Event()
        self.server_socket = server_socket
        self.llm_client_overrides = (
            llm_client_overrides if llm_client_overrides is not None else {}
        )
        self.database_engine: AsyncEngine | None = None

        # Initialize all instance attributes
        self.fastapi_app: FastAPI | None = None
        self.shared_httpx_client: httpx.AsyncClient | None = None
        self.embedding_generator: EmbeddingGenerator | None = None
        self.processing_services_registry: dict[str, ProcessingService] = {}
        self.default_processing_service: ProcessingService | None = None
        self.scraper_instance: PlaywrightScraper | None = None
        self.attachment_registry: AttachmentRegistry | None = None
        self.document_indexer: DocumentIndexer | None = None
        self.email_indexer: EmailIndexer | None = None
        self.notes_indexer: NotesIndexer | None = None
        self.telegram_service: TelegramService | None = None
        self.push_notification_service: PushNotificationService | None = None
        self.task_worker_instance: TaskWorker | None = None
        self.task_worker_task: asyncio.Task | None = None  # Track the worker task
        self.uvicorn_server_task: asyncio.Task | None = None
        self.health_monitor_task: asyncio.Task | None = None  # Track health monitor
        self.event_processor_task: asyncio.Task | None = None  # Track event processor
        self._is_shutdown_complete = False

        # Event system
        self.event_processor: EventProcessor | None = None
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        self.home_assistant_clients: dict[str, Any] = {}  # profile_id -> HA client

        # Logging handler
        self.error_logging_handler = None

    async def _get_db_context_for_provider(self) -> DatabaseContext:
        """Provides database context for context providers."""
        if not self.database_engine:
            raise RuntimeError("Database engine not initialized")
        return get_db_context(self.database_engine)

    def _get_db_context_for_telegram(self) -> DatabaseContext:
        """Provides database context for Telegram service."""
        if not self.database_engine:
            raise RuntimeError("Database engine not initialized")
        return get_db_context(self.database_engine)

    def _get_db_context_for_events(self) -> DatabaseContext:
        """Provides database context for event system."""
        if not self.database_engine:
            raise RuntimeError("Database engine not initialized")
        return get_db_context(self.database_engine)

    async def _ensure_playwright_browsers_installed(self) -> None:
        """Ensure Playwright browsers are installed, install if missing."""
        try:
            # Check if browsers are installed by trying to get the path
            dry_run_process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "playwright",
                "install",
                "--dry-run",
                stdout=asyncio_subprocess.PIPE,
                stderr=asyncio_subprocess.PIPE,
            )

            try:
                dry_run_stdout, dry_run_stderr = await asyncio.wait_for(
                    dry_run_process.communicate(), timeout=10
                )
            except TimeoutError:
                dry_run_process.kill()
                await dry_run_process.communicate()
                logger.warning("Playwright browser check timed out")
                return

            dry_run_output = (dry_run_stdout or b"").decode()
            needs_install = "chromium" in dry_run_output.lower()

            if dry_run_process.returncode != 0:
                needs_install = True
                dry_run_error_output = (dry_run_stderr or b"").decode().strip()
                if dry_run_error_output:
                    logger.debug(
                        "Playwright dry-run returned non-zero exit code: %s",
                        dry_run_error_output,
                    )

            # If dry-run suggests installation is needed, install chromium
            if needs_install:
                logger.info("Playwright browsers not found, installing chromium...")
                install_process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "playwright",
                    "install",
                    "chromium",
                    stdout=asyncio_subprocess.PIPE,
                    stderr=asyncio_subprocess.PIPE,
                )

                try:
                    _, install_stderr = await asyncio.wait_for(
                        install_process.communicate(), timeout=300
                    )
                except TimeoutError:
                    install_process.kill()
                    await install_process.communicate()
                    logger.warning("Playwright browser installation timed out")
                    return

                if install_process.returncode == 0:
                    logger.info("Playwright chromium browser installed successfully")
                else:
                    install_error_output = (install_stderr or b"").decode().strip()
                    logger.warning(
                        "Failed to install Playwright browsers: %s",
                        install_error_output,
                    )
            else:
                logger.debug("Playwright browsers already installed")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not check/install Playwright browsers: {e}")

    async def setup_dependencies(self) -> None:
        """Initializes and wires up all core application components."""
        # Ensure Playwright browsers are installed as a failsafe
        await self._ensure_playwright_browsers_installed()

        logger.info(f"Using model: {self.config.model}")

        # Create FastAPI app instance
        self.fastapi_app = create_app()
        logger.info("Created FastAPI app instance")

        # Store config in FastAPI app state for access by routes
        self.fastapi_app.state.config = self.config
        logger.info("Stored configuration in FastAPI app state.")

        # Create MessageNotifier for live message updates
        message_notifier = MessageNotifier()
        self.fastapi_app.state.message_notifier = message_notifier
        logger.info("MessageNotifier created and stored in FastAPI app state")

        # Store shutdown event for SSE and other async endpoints
        self.fastapi_app.state.shutdown_event = self.shutdown_event

        # Initialize chat_interfaces registry for cross-interface messaging
        self.fastapi_app.state.chat_interfaces = {}
        logger.info("Chat interfaces registry initialized")

        self.shared_httpx_client = httpx.AsyncClient()
        logger.info("Shared httpx.AsyncClient created.")

        # Check if Telegram is enabled
        self.telegram_enabled = self.config.telegram_enabled

        if self.telegram_enabled and not self.config.telegram_token:
            raise ValueError(
                "Telegram Bot Token is missing when telegram_enabled=True."
            )

        selected_model = self.config.model
        if selected_model.startswith("gemini/"):
            if not os.getenv("GEMINI_API_KEY"):
                raise ValueError("Gemini API Key is missing (GEMINI_API_KEY env var).")
            logger.info("Gemini model selected. Using GEMINI_API_KEY from environment.")
        elif selected_model.startswith("openrouter/"):
            if not self.config.openrouter_api_key:
                raise ValueError("OpenRouter API Key is missing.")
            if self.config.openrouter_api_key:
                os.environ["OPENROUTER_API_KEY"] = self.config.openrouter_api_key
            logger.info(
                "OpenRouter model selected. OPENROUTER_API_KEY set for LiteLLM."
            )
        else:
            logger.warning(
                f"No specific API key validation for model: {selected_model}."
            )

        embedding_model_name = self.config.embedding_model
        embedding_dimensions = self.config.embedding_dimensions
        if embedding_model_name == "mock-deterministic-embedder":
            self.embedding_generator = embeddings.MockEmbeddingGenerator(
                model_name=embedding_model_name,
                dimensions=embedding_dimensions,
                default_embedding_behavior="generate",
            )
        elif embedding_model_name.startswith("/") or embedding_model_name in {
            "all-MiniLM-L6-v2",
            "other-local-model-name",
        }:
            try:
                if "SentenceTransformerEmbeddingGenerator" not in dir(embeddings):
                    raise ImportError("sentence-transformers library not installed.")
                self.embedding_generator = (
                    embeddings.SentenceTransformerEmbeddingGenerator(
                        model_name_or_path=embedding_model_name
                    )
                )
            except Exception as e:
                logger.critical(
                    f"Failed to initialize local embedding model '{embedding_model_name}': {e}"
                )
                raise SystemExit(f"Local embedding model init failed: {e}") from e
        else:
            self.embedding_generator = LiteLLMEmbeddingGenerator(
                model=embedding_model_name, dimensions=embedding_dimensions
            )
        logger.info(
            f"Using embedding generator: {type(self.embedding_generator).__name__} with model: {self.embedding_generator.model_name}"
        )
        self.fastapi_app.state.embedding_generator = self.embedding_generator

        # Create database engine
        # Use injected engine if provided, otherwise create from config
        if self._injected_database_engine:
            self.database_engine = self._injected_database_engine
            logger.info("Using injected database engine")
        else:
            database_url = self.config.database_url
            self.database_engine = create_engine_with_sqlite_optimizations(database_url)
            logger.info(f"Database engine created for URL: {database_url}")

            # Initialize database only when we create our own engine
            await init_db(self.database_engine)
            async with get_db_context(self.database_engine) as db_ctx:
                await db_ctx.init_vector_db()

        # Store engine in FastAPI app state for web dependencies
        self.fastapi_app.state.database_engine = self.database_engine

        # Configure authentication with the database engine
        configure_app_auth(self.fastapi_app, self.database_engine)
        logger.info("Authentication configured with database engine")

        # Initialize AttachmentRegistry (consolidates file storage and database metadata)
        # Must come after database engine initialization
        # Prefer chat_attachment_storage_path, fall back to attachment_config.storage_path
        attachment_storage_path = (
            self.config.chat_attachment_storage_path
            or self.config.attachment_config.storage_path
        )
        attachment_config = self.config.attachment_config

        # Import locally to avoid circular imports
        from family_assistant.services.attachment_registry import (  # noqa: PLC0415
            AttachmentRegistry,
        )

        self.attachment_registry = AttachmentRegistry(
            storage_path=attachment_storage_path,
            db_engine=self.database_engine,
            config=attachment_config.model_dump(),
        )

        # Store in FastAPI app state for web access
        self.fastapi_app.state.attachment_registry = self.attachment_registry
        logger.info(
            f"AttachmentRegistry initialized with path: {attachment_storage_path}"
        )

        # Initialize PushNotificationService
        vapid_private_key = self.config.pwa_config.vapid_private_key
        vapid_contact_email = self.config.pwa_config.vapid_contact_email

        self.push_notification_service = PushNotificationService(
            vapid_private_key=vapid_private_key,
            vapid_contact_email=vapid_contact_email,
        )

        # Store in app.state for lifespan to retrieve
        self.fastapi_app.state.push_notification_service = (
            self.push_notification_service
        )
        logger.info(
            f"PushNotificationService initialized (enabled={self.push_notification_service.enabled})"
        )

        # Setup error logging to database if enabled
        error_logging_enabled = self.config.logging.database_errors.enabled
        # Also check environment variable to disable for testing
        if error_logging_enabled and not os.environ.get(
            "FAMILY_ASSISTANT_DISABLE_DB_ERROR_LOGGING"
        ):
            self.error_logging_handler = setup_error_logging(self.database_engine)
            logger.info("Database error logging handler initialized")

        resolved_profiles = self.config.service_profiles
        default_service_profile_id = self.config.default_service_profile_id

        available_doc_files = _scan_user_docs()
        formatted_doc_list_for_tool_desc = ", ".join(available_doc_files) or "None"
        base_local_tools_definition = copy.deepcopy(local_tools_definition)

        # Prepare the string listing available service profiles and their descriptions
        profile_descriptions_list = []
        for profile_config_item in resolved_profiles:
            profile_id_item = profile_config_item.id
            description_item = (
                profile_config_item.description or "No description available."
            )
            profile_descriptions_list.append(
                f"- ID: {profile_id_item}, Description: {description_item}"
            )
        available_service_profiles_with_descriptions_str = "\n".join(
            profile_descriptions_list
        )
        if not available_service_profiles_with_descriptions_str:
            available_service_profiles_with_descriptions_str = (
                "No specific service profiles are currently described."
            )

        # Update the description of the delegate_to_service tool in the base definition list
        # This ensures all profiles get the fully described delegate_to_service tool.
        for tool_def_template in base_local_tools_definition:
            if (
                tool_def_template.get("function", {}).get("name")
                == "delegate_to_service"
            ):
                original_description = tool_def_template["function"].get(
                    "description", ""
                )
                if (
                    "{available_service_profiles_with_descriptions}"
                    in original_description
                ):
                    tool_def_template["function"]["description"] = (
                        original_description.format(
                            available_service_profiles_with_descriptions=(
                                available_service_profiles_with_descriptions_str
                            )
                        )
                    )
                    logger.debug(
                        "Updated delegate_to_service tool description with profile list in base_local_tools_definition."
                    )
                else:
                    logger.warning(
                        "Placeholder for service profiles not found in delegate_to_service tool description in base_local_tools_definition."
                    )
                break

        # Create root providers with ALL tools for UI/API access
        logger.info("Creating root ToolsProvider with all available tools")

        # Create root local provider with ALL tools
        root_local_provider = LocalToolsProvider(
            definitions=base_local_tools_definition,  # ALL local tools
            implementations=local_tool_implementations,  # ALL implementations
            embedding_generator=self.embedding_generator,
            calendar_config=_calendar_config_to_dict(self.config.calendar_config),
        )

        # Create root MCP provider with ALL configured servers
        all_mcp_servers_config = {
            server_id: server_config.model_dump()
            for server_id, server_config in self.config.mcp_config.mcpServers.items()
        }
        root_mcp_provider = MCPToolsProvider(
            mcp_server_configs=all_mcp_servers_config,
            initialization_timeout_seconds=60,
        )

        # Create composite root provider
        self.root_tools_provider = CompositeToolsProvider(
            providers=[root_local_provider, root_mcp_provider]
        )

        # Initialize and store for UI/API access
        await self.root_tools_provider.get_tool_definitions()
        self.fastapi_app.state.tools_provider = self.root_tools_provider
        self.fastapi_app.state.tool_definitions = (
            await self.root_tools_provider.get_tool_definitions()
        )
        logger.info(
            f"Root ToolsProvider initialized with {len(self.fastapi_app.state.tool_definitions)} tools"
        )

        for profile_conf in resolved_profiles:
            profile_id = profile_conf.id
            logger.info(
                f"Initializing ProcessingService for profile ID: '{profile_id}'"
            )
            profile_proc_conf = profile_conf.processing_config
            profile_tools_conf = profile_conf.tools_config
            profile_chat_id_map = profile_conf.chat_id_to_name_map

            profile_llm_model = profile_proc_conf.llm_model or self.config.model

            if profile_id in self.llm_client_overrides:
                llm_client_for_profile = self.llm_client_overrides[profile_id]
                logger.info(
                    f"Profile '{profile_id}' using overridden LLM client: {type(llm_client_for_profile).__name__}"
                )
            else:
                # Check if using retry_config format
                if profile_proc_conf.retry_config is not None:
                    # Direct retry_config format - convert to dict for LLMClientFactory
                    retry_config_dict = profile_proc_conf.retry_config.model_dump(
                        exclude_none=True
                    )
                    # Add shared llm_parameters as model_parameters to both primary and fallback
                    # if not already specified. model_parameters is used for pattern-based
                    # parameter matching (e.g., "openrouter/google/gemini-" -> {params})
                    llm_params = self.config.llm_parameters
                    if (
                        "primary" in retry_config_dict
                        and "model_parameters" not in retry_config_dict["primary"]
                    ):
                        retry_config_dict["primary"]["model_parameters"] = llm_params
                    if (
                        "fallback" in retry_config_dict
                        and retry_config_dict["fallback"]
                        and "model_parameters" not in retry_config_dict["fallback"]
                    ):
                        retry_config_dict["fallback"]["model_parameters"] = llm_params

                    # Wrap in a config dict with retry_config key
                    # ast-grep-ignore: no-dict-any - Temporary dict passed to RetryingLLMClient
                    client_config: dict[str, Any] = {"retry_config": retry_config_dict}

                    primary_model = retry_config_dict.get("primary", {}).get("model")
                    fallback_model = (
                        retry_config_dict.get("fallback", {}).get("model")
                        if retry_config_dict.get("fallback")
                        else None
                    )
                    logger.info(
                        f"Creating RetryingLLMClient for profile '{profile_id}' with primary='{primary_model}', "
                        f"fallback='{fallback_model}'"
                    )
                else:
                    # Simple configuration without retry
                    provider = profile_proc_conf.provider or "litellm"
                    client_config = {
                        "model": profile_llm_model,
                        "provider": provider,
                        "model_parameters": self.config.llm_parameters,
                    }

                    logger.info(
                        f"Creating LLM client for profile '{profile_id}' with provider='{provider}', model='{profile_llm_model}'"
                    )

                llm_client_for_profile = LLMClientFactory.create_client(
                    config=client_config
                )
                logger.info(
                    f"Profile '{profile_id}' using client: {type(llm_client_for_profile).__name__}"
                )

            local_tools_list_from_config = profile_tools_conf.enable_local_tools
            enabled_local_tool_names = (
                set(local_tool_implementations.keys())
                if local_tools_list_from_config is None
                else set(local_tools_list_from_config)
            )

            profile_specific_local_definitions = []
            for tool_def_template in base_local_tools_definition:
                tool_name = tool_def_template.get("function", {}).get("name")
                if tool_name in enabled_local_tool_names:
                    current_tool_def = copy.deepcopy(tool_def_template)
                    if tool_name == "get_user_documentation_content":
                        try:
                            current_tool_def["function"]["description"] = (
                                current_tool_def["function"]["description"].format(
                                    available_doc_files=formatted_doc_list_for_tool_desc
                                )
                            )
                        except KeyError as e:
                            logger.error(
                                f"Failed to format doc tool description for profile {profile_id}: {e}"
                            )
                    profile_specific_local_definitions.append(current_tool_def)

            # Build complete set of allowed tools (local + MCP)
            mcp_server_ids_from_config = profile_tools_conf.enable_mcp_server_ids

            # Start with local tools
            all_enabled_tool_names = enabled_local_tool_names.copy()

            # Add MCP tools from enabled servers
            if mcp_server_ids_from_config is not None:
                # Get MCP tool-to-server mapping
                mcp_tool_to_server = root_mcp_provider.get_tool_to_server_mapping()

                # Add tools from enabled MCP servers
                for tool_name, server_id in mcp_tool_to_server.items():
                    if server_id in mcp_server_ids_from_config:
                        all_enabled_tool_names.add(tool_name)

                logger.info(
                    f"Profile '{profile_id}' has {len(enabled_local_tool_names)} local tools "
                    f"and {len(all_enabled_tool_names) - len(enabled_local_tool_names)} MCP tools "
                    f"from servers: {mcp_server_ids_from_config}"
                )

            # Create filtered view of root provider for this profile
            if (
                local_tools_list_from_config is None
                and mcp_server_ids_from_config is None
            ):
                # All tools enabled for this profile
                filtered_provider = self.root_tools_provider
            else:
                # Filter to only allowed tools
                filtered_provider = FilteredToolsProvider(
                    wrapped_provider=self.root_tools_provider,
                    allowed_tool_names=all_enabled_tool_names,
                )
            profile_confirm_tools_set = set(profile_tools_conf.confirm_tools)
            logger.debug(
                f"Assistant: Profile {profile_id} confirm_tools from config: {profile_tools_conf.confirm_tools}"
            )
            logger.debug(
                f"Assistant: Profile {profile_id} confirm_tools_set: {profile_confirm_tools_set}"
            )
            # Get confirmation timeout from config, default to 3600 seconds (1 hour)
            confirmation_timeout = profile_tools_conf.confirmation_timeout_seconds
            confirming_provider_for_profile = ConfirmingToolsProvider(
                wrapped_provider=filtered_provider,
                tools_requiring_confirmation=profile_confirm_tools_set,
                confirmation_timeout=confirmation_timeout,
            )
            await confirming_provider_for_profile.get_tool_definitions()

            notes_provider = NotesContextProvider(
                get_db_context_func=self._get_db_context_for_provider,
                prompts=profile_proc_conf.prompts,
                attachment_registry=self.attachment_registry,
            )
            calendar_provider = CalendarContextProvider(
                calendar_config=_calendar_config_to_dict(self.config.calendar_config),
                timezone_str=profile_proc_conf.timezone,
                prompts=profile_proc_conf.prompts,
            )
            known_users_provider = KnownUsersContextProvider(
                chat_id_to_name_map=profile_chat_id_map,
                prompts=profile_proc_conf.prompts,
            )
            context_providers = [
                notes_provider,
                calendar_provider,
                known_users_provider,
            ]

            willyweather_api_key = self.config.willyweather_api_key
            willyweather_location_id = self.config.willyweather_location_id
            if (
                willyweather_api_key
                and willyweather_location_id
                and self.shared_httpx_client
            ):
                weather_provider = WeatherContextProvider(
                    location_id=willyweather_location_id,
                    api_key=willyweather_api_key,
                    prompts=profile_proc_conf.prompts,
                    timezone_str=profile_proc_conf.timezone,
                    httpx_client=self.shared_httpx_client,
                )
                context_providers.append(weather_provider)

            # --- Home Assistant Context Provider ---
            ha_api_url = profile_proc_conf.home_assistant_api_url
            ha_token = profile_proc_conf.home_assistant_token
            ha_template = profile_proc_conf.home_assistant_context_template
            ha_verify_ssl = profile_proc_conf.home_assistant_verify_ssl

            if ha_api_url and ha_token:
                # Create or reuse Home Assistant client
                ha_client_key = f"{ha_api_url}:{ha_token[:8]}..."  # Key for caching
                if ha_client_key not in self.home_assistant_clients:
                    ha_client = create_home_assistant_client(
                        api_url=ha_api_url,
                        token=ha_token,
                        verify_ssl=ha_verify_ssl,
                    )
                    if ha_client:
                        self.home_assistant_clients[ha_client_key] = ha_client
                        self.home_assistant_clients[profile_id] = (
                            ha_client  # Also store by profile
                        )
                else:
                    ha_client = self.home_assistant_clients[ha_client_key]
                    self.home_assistant_clients[profile_id] = (
                        ha_client  # Also store by profile
                    )

                if ha_client and ha_template:
                    try:
                        # Local import to ensure homeassistant_api is only required if configured
                        # The main import is already guarded in context_providers.py
                        if (
                            HomeAssistantContextProvider.__module__
                            == "family_assistant.context_providers"
                        ):  # Check it's our class
                            home_assistant_provider = HomeAssistantContextProvider(
                                api_url=ha_api_url,
                                token=ha_token,
                                context_template=ha_template,
                                prompts=profile_proc_conf.prompts,
                                verify_ssl=ha_verify_ssl,
                                client=ha_client,
                            )
                            context_providers.append(home_assistant_provider)
                            logger.info(
                                f"HomeAssistantContextProvider added for profile '{profile_id}'."
                            )
                    except ImportError:  # This case should ideally be handled by the check in context_providers.py
                        logger.warning(
                            "homeassistant_api library is not installed, but Home Assistant context provider is configured. Skipping."
                        )
                    except Exception as e:
                        logger.error(
                            f"Failed to initialize HomeAssistantContextProvider for profile '{profile_id}': {e}",
                            exc_info=True,
                        )
                elif ha_api_url or ha_token or ha_template:
                    logger.warning(
                        f"Home Assistant context provider for profile '{profile_id}' is partially configured "
                        "but missing essential settings (URL, token, or template). Skipping."
                    )
            # --- End Home Assistant Context Provider ---

            service_config = ProcessingServiceConfig(
                prompts=profile_proc_conf.prompts,
                timezone_str=profile_proc_conf.timezone,
                max_history_messages=profile_proc_conf.max_history_messages,
                history_max_age_hours=profile_proc_conf.history_max_age_hours,
                web_max_history_messages=profile_proc_conf.web_max_history_messages,
                web_history_max_age_hours=profile_proc_conf.web_history_max_age_hours,
                max_iterations=profile_proc_conf.max_iterations,
                tools_config=profile_tools_conf.model_dump(),
                delegation_security_level=profile_proc_conf.delegation_security_level,
                id=profile_id,
                description=profile_conf.description
                or f"Processing profile: {profile_id}",
            )

            processing_service_instance = ProcessingService(
                llm_client=llm_client_for_profile,
                tools_provider=confirming_provider_for_profile,
                service_config=service_config,
                context_providers=context_providers,
                server_url=self.config.server_url,
                app_config=self.config,
                attachment_registry=self.attachment_registry,
                event_sources=self.event_processor.sources
                if self.event_processor
                else None,
            )
            # Set the home_assistant_client if available for this profile
            if profile_id in self.home_assistant_clients:
                processing_service_instance.home_assistant_client = (
                    self.home_assistant_clients[profile_id]
                )

            # Set camera backend if configured for this profile
            camera_config = profile_proc_conf.camera_config
            if camera_config:
                backend_type = camera_config.backend
                if backend_type == "reolink":
                    try:
                        from family_assistant.camera.reolink import (  # noqa: PLC0415
                            create_reolink_backend,
                        )

                        # Pass typed config directly
                        camera_backend = create_reolink_backend(
                            camera_config.cameras_config or None
                        )
                        if camera_backend:
                            processing_service_instance.camera_backend = camera_backend
                            logger.info(
                                f"Camera backend initialized for profile '{profile_id}'"
                            )
                        else:
                            logger.warning(
                                f"Camera backend not created for profile '{profile_id}' "
                                "(no config or reolink-aio unavailable)"
                            )
                    except ImportError:
                        logger.warning(
                            "Reolink backend requested but reolink-aio not installed"
                        )
                    except Exception:
                        logger.exception(
                            f"Failed to create camera backend for profile '{profile_id}'"
                        )

            self.processing_services_registry[profile_id] = processing_service_instance

        if not self.processing_services_registry:
            logger.critical("No processing service profiles initialized.")
            raise SystemExit("No processing service profiles initialized.")

        for service_instance in self.processing_services_registry.values():
            service_instance.set_processing_services_registry(
                self.processing_services_registry
            )

        self.fastapi_app.state.processing_services = self.processing_services_registry

        self.default_processing_service = self.processing_services_registry.get(
            default_service_profile_id
        )
        if not self.default_processing_service:
            logger.warning(
                f"Default service profile ID '{default_service_profile_id}' not found. Falling back to first available."
            )
            default_service_profile_id = next(
                iter(self.processing_services_registry.keys())
            )
            self.default_processing_service = self.processing_services_registry[
                default_service_profile_id
            ]

        self.fastapi_app.state.processing_service = self.default_processing_service
        self.fastapi_app.state.llm_client = self.default_processing_service.llm_client
        # Note: tools_provider and tool_definitions are already set to root provider above
        logger.info(
            f"Default processing service set to profile ID: '{default_service_profile_id}'."
        )

        self.scraper_instance = PlaywrightScraper()
        self.fastapi_app.state.scraper = self.scraper_instance

        pipeline_config = self.config.indexing_pipeline_config.model_dump()
        if not pipeline_config.get("processors"):
            logger.warning("No processors in 'indexing_pipeline_config'.")

        self.document_indexer = DocumentIndexer(
            pipeline_config=pipeline_config,
            llm_client=self.default_processing_service.llm_client,
            embedding_generator=self.embedding_generator,
            scraper=self.scraper_instance,
        )
        self.email_indexer = EmailIndexer(pipeline=self.document_indexer.pipeline)
        self.notes_indexer = NotesIndexer(pipeline=self.document_indexer.pipeline)
        logger.info("DocumentIndexer, EmailIndexer, and NotesIndexer initialized.")

        # Instantiate TelegramService in setup_dependencies but don't start polling yet
        if not self.default_processing_service:  # Should be set by now
            raise RuntimeError(
                "Default processing service not available for TelegramService setup."
            )

        # Only initialize Telegram service if enabled
        if self.telegram_enabled:
            assert self.database_engine is not None, (
                "Database engine must be initialized before creating TelegramService"
            )
            # telegram_token is verified earlier when telegram_enabled is True
            assert self.config.telegram_token is not None
            self.telegram_service = TelegramService(
                telegram_token=self.config.telegram_token,
                allowed_user_ids=self.config.allowed_user_ids,
                developer_chat_id=self.config.developer_chat_id,
                processing_service=self.default_processing_service,
                processing_services_registry=self.processing_services_registry,
                app_config=self.config,
                attachment_registry=self.attachment_registry,
                get_db_context_func=self._get_db_context_for_telegram,
                fastapi_app=self.fastapi_app,  # Pass FastAPI app for chat_interfaces access
                # use_batching argument removed
            )
            self.fastapi_app.state.telegram_service = self.telegram_service
            # Register telegram chat interface in the registry
            self.fastapi_app.state.chat_interfaces["telegram"] = (
                self.telegram_service.chat_interface
            )
            logger.info(
                "TelegramService instantiated and stored in FastAPI app state during setup_dependencies."
            )
        else:
            self.telegram_service = None
            self.fastapi_app.state.telegram_service = None
            logger.info("Telegram service disabled (telegram_enabled=False)")

        # Initialize event system if enabled
        event_config = self.config.event_system
        if event_config.enabled:
            event_sources = {}  # Dict, not list

            # Create Home Assistant event sources for unique HA instances
            if event_config.sources.home_assistant.enabled:
                # Get unique HA clients (use cache keys which represent unique instances)
                unique_clients = {}
                for key, ha_client in self.home_assistant_clients.items():
                    # Cache keys contain "..." and represent unique HA instances
                    if "..." in str(key):
                        unique_clients[key] = ha_client

                # Create one event source per unique HA instance
                for idx, (key, ha_client) in enumerate(unique_clients.items()):
                    logger.info(f"Creating HomeAssistantSource for HA instance: {key}")
                    ha_source = HomeAssistantSource(client=ha_client)
                    # Use a simple numeric suffix if we have multiple HA instances
                    source_key = (
                        "home_assistant" if idx == 0 else f"home_assistant_{idx}"
                    )
                    event_sources[source_key] = ha_source

            # Always add indexing source since it's needed for document indexing events
            self.indexing_source = IndexingSource()
            event_sources["indexing"] = self.indexing_source
            logger.info("Created IndexingSource for document indexing events")

            if event_sources:
                sample_interval_hours = event_config.storage.sample_interval_hours

                assert self.database_engine is not None, (
                    "Database engine must be initialized before creating EventProcessor"
                )
                self.event_processor = EventProcessor(
                    sources=event_sources,
                    sample_interval_hours=sample_interval_hours,
                    config=event_config.model_dump(),  # Convert to dict for backward compatibility
                    get_db_context_func=self._get_db_context_for_events,
                    # db_context will be created internally if not provided
                )
                logger.info(
                    f"Event processor initialized with {len(event_sources)} sources"
                )
            else:
                logger.info("Event system enabled but no event sources configured")

    async def start_services(self) -> None:
        """Starts all long-running services and waits for shutdown."""
        if not self.default_processing_service or not self.embedding_generator:
            raise RuntimeError("Dependencies not set up before starting services.")
        assert self.fastapi_app is not None, "FastAPI app not initialized"

        # Only start Telegram polling if enabled
        if self.telegram_enabled:
            if not self.telegram_service:
                raise RuntimeError(
                    "TelegramService not initialized before starting services."
                )
            await self.telegram_service.start_polling()
            logger.info("TelegramService polling started.")
        else:
            logger.info("Telegram service disabled, skipping polling.")

        # Get port from config, default to 8000
        server_port = self.config.server_port

        if self.server_socket is not None:
            # Use the pre-bound socket to avoid race conditions
            uvicorn_config = uvicorn.Config(
                self.fastapi_app, fd=self.server_socket.fileno(), log_level="info"
            )
            logger.info(f"Web server using pre-bound socket on port {server_port}")
        else:
            # Use normal host/port binding
            uvicorn_config = uvicorn.Config(
                self.fastapi_app, host="0.0.0.0", port=server_port, log_level="info"
            )
            logger.info(f"Web server running on http://0.0.0.0:{server_port}")

        server = uvicorn.Server(uvicorn_config)
        self.uvicorn_server_task = asyncio.create_task(server.serve())

        logger.info(
            "In development, run 'poe dev' and access the app at http://localhost:5173"
        )

        default_profile_conf = next(
            p
            for p in self.config.service_profiles
            if p.id == self.default_processing_service.service_config.id
        )

        self.task_worker_instance = TaskWorker(
            processing_service=self.default_processing_service,
            chat_interface=self.telegram_service.chat_interface
            if self.telegram_service
            else NullChatInterface(),
            calendar_config=_calendar_config_to_dict(self.config.calendar_config),
            timezone_str=default_profile_conf.processing_config.timezone,
            embedding_generator=self.embedding_generator,
            # shutdown_event is likely handled internally by TaskWorker or passed differently
            indexing_source=getattr(
                self, "indexing_source", None
            ),  # Pass indexing source if available
            event_sources=self.event_processor.sources
            if self.event_processor
            else None,
            engine=self.database_engine,  # Pass the database engine
        )
        self.task_worker_instance.register_task_handler(
            "log_message", task_wrapper_handle_log_message
        )
        if self.document_indexer:
            self.task_worker_instance.register_task_handler(
                "process_uploaded_document", self.document_indexer.process_document
            )
        if self.email_indexer:
            self.task_worker_instance.register_task_handler(
                "index_email", self.email_indexer.handle_index_email
            )
        if self.notes_indexer:
            self.task_worker_instance.register_task_handler(
                "index_note", self.notes_indexer.handle_index_note
            )
        self.task_worker_instance.register_task_handler(
            "llm_callback", handle_llm_callback
        )
        self.task_worker_instance.register_task_handler(
            "embed_and_store_batch", handle_embed_and_store_batch
        )
        self.task_worker_instance.register_task_handler(
            "system_event_cleanup", handle_system_event_cleanup
        )
        self.task_worker_instance.register_task_handler(
            "system_error_log_cleanup", handle_system_error_log_cleanup
        )
        self.task_worker_instance.register_task_handler(
            "script_execution", handle_script_execution
        )
        self.task_worker_instance.register_task_handler(
            "reindex_document", self.handle_reindex_document
        )
        logger.info(
            f"Registered task handlers for worker {self.task_worker_instance.worker_id}"
        )
        self.task_worker_task = asyncio.create_task(self.task_worker_instance.run())

        # Start health monitoring for the task worker
        self.health_monitor_task = asyncio.create_task(
            self._monitor_task_worker_health()
        )

        # Start event processor if initialized
        if self.event_processor:
            self.event_processor_task = asyncio.create_task(
                self.event_processor.start()
            )
            logger.info("Event processor started")

            # Create system cleanup task
            await self._setup_system_tasks()

        await self.shutdown_event.wait()
        logger.info("Shutdown signal received by Assistant. Stopping services...")

        if server.started and self.uvicorn_server_task:
            server.should_exit = True
            await self.uvicorn_server_task
            logger.info("Web server stopped.")

        # Final cleanup will be in stop_services, called from main's finally block.

    def initiate_shutdown(self, signal_name: str) -> None:
        """Sets the shutdown event to begin graceful shutdown."""
        if not self.shutdown_event.is_set():
            logger.warning(
                f"Received signal {signal_name}. Initiating shutdown via Assistant..."
            )
            self.shutdown_event.set()
        else:
            logger.warning(
                f"Shutdown already in progress. Signal {signal_name} received again."
            )

    async def _setup_system_tasks(self) -> None:
        """Upsert system tasks on startup."""
        try:
            assert self.database_engine is not None, (
                "Database engine must be initialized before setting up system tasks"
            )
            async with get_db_context(self.database_engine) as db_ctx:
                # Get the timezone from the default profile
                if not self.default_processing_service:
                    logger.error(
                        "Default processing service not available for system tasks setup"
                    )
                    return

                default_profile_conf = next(
                    p
                    for p in self.config.service_profiles
                    if p.id == self.default_processing_service.service_config.id
                )
                timezone_str = default_profile_conf.processing_config.timezone
                local_tz = zoneinfo.ZoneInfo(timezone_str)

                # Get current time in local timezone and calculate next 3 AM local time
                now_local = datetime.now(local_tz)
                next_3am_local = now_local.replace(
                    hour=3, minute=0, second=0, microsecond=0
                )

                # If it's already past 3 AM today, schedule for tomorrow
                if now_local >= next_3am_local:
                    next_3am_local += timedelta(days=1)

                # Convert to UTC for storage
                next_3am_utc = next_3am_local.astimezone(UTC)

                # Upsert the system event cleanup task
                try:
                    await db_ctx.tasks.enqueue(
                        task_id="system_event_cleanup_daily",
                        task_type="system_event_cleanup",
                        payload={"retention_hours": 48},
                        scheduled_at=next_3am_utc,
                        recurrence_rule="FREQ=DAILY;BYHOUR=3;BYMINUTE=0",
                        max_retries_override=5,  # Higher retry count for system tasks
                    )
                    logger.info(
                        f"System event cleanup task scheduled for {next_3am_local} ({timezone_str})"
                    )
                except Exception as e:
                    # If task already exists, this is fine - just log it
                    logger.info(f"System event cleanup task setup: {e}")

                # Upsert the error log cleanup task
                try:
                    # Get retention days from config
                    error_log_retention_days = (
                        self.config.logging.database_errors.retention_days
                    )

                    await db_ctx.tasks.enqueue(
                        task_id="system_error_log_cleanup_daily",
                        task_type="system_error_log_cleanup",
                        payload={"retention_days": error_log_retention_days},
                        scheduled_at=next_3am_utc,
                        recurrence_rule="FREQ=DAILY;BYHOUR=3;BYMINUTE=0",
                        max_retries_override=5,  # Higher retry count for system tasks
                    )
                    logger.info(
                        f"System error log cleanup task scheduled for {next_3am_local} ({timezone_str}) with {error_log_retention_days} day retention"
                    )
                except Exception as e:
                    # If task already exists, this is fine - just log it
                    logger.info(f"System error log cleanup task setup: {e}")
        except RuntimeError as e:
            if "different loop" in str(e):
                logger.warning(
                    "Skipping system tasks setup due to event loop mismatch. "
                    "This can happen during startup and tasks will be set up on next restart."
                )
            else:
                logger.error(f"Failed to setup system tasks: {e}")
        except Exception as e:
            logger.error(f"Failed to setup system tasks: {e}")

    async def _monitor_task_worker_health(self) -> None:
        """Monitors the health of the task worker and restarts it if necessary."""
        HEALTH_CHECK_INTERVAL = 30  # Check every 30 seconds
        WORKER_INACTIVITY_TIMEOUT = 600

        while not self.shutdown_event.is_set():
            try:
                await asyncio.sleep(HEALTH_CHECK_INTERVAL)

                if not self.task_worker_instance or not self.task_worker_task:
                    continue

                # Check if the task is still running
                if self.task_worker_task.done():
                    # Task has completed or failed
                    try:
                        # This will raise any exception that occurred in the task
                        self.task_worker_task.result()
                        logger.warning("Task worker exited normally, restarting...")
                    except Exception as e:
                        logger.error(
                            f"Task worker crashed with error: {e}", exc_info=True
                        )

                    # Restart the worker
                    logger.info("Restarting task worker...")
                    self.task_worker_task = asyncio.create_task(
                        self.task_worker_instance.run()
                    )
                    continue

                # Check last activity time
                if self.task_worker_instance.last_activity:
                    time_since_activity = (
                        datetime.now(UTC) - self.task_worker_instance.last_activity
                    ).total_seconds()

                    if time_since_activity > WORKER_INACTIVITY_TIMEOUT:
                        logger.error(
                            f"Task worker appears stuck (no activity for {time_since_activity:.0f}s), "
                            "cancelling and restarting..."
                        )
                        self.task_worker_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await self.task_worker_task

                        # Restart the worker
                        self.task_worker_task = asyncio.create_task(
                            self.task_worker_instance.run()
                        )
                        logger.info("Task worker restarted after inactivity timeout")

            except asyncio.CancelledError:
                # Shutdown requested
                break
            except Exception as e:
                logger.error(f"Error in task worker health monitor: {e}", exc_info=True)
                await asyncio.sleep(HEALTH_CHECK_INTERVAL)

        logger.info("Task worker health monitor stopped")

    async def stop_services(self) -> None:
        """Gracefully stops all managed services."""
        if self._is_shutdown_complete:
            logger.info("stop_services already completed.")
            return

        logger.info("Assistant stop_services called.")
        # Ensure shutdown_event is set, in case stop_services is called directly
        if not self.shutdown_event.is_set():
            self.shutdown_event.set()

        # Cancel only the background tasks we own (not all tasks in the event loop)
        # This prevents interfering with pytest-xdist workers and other infrastructure
        owned_tasks = []
        if self.health_monitor_task and not self.health_monitor_task.done():
            owned_tasks.append(self.health_monitor_task)
        if self.event_processor_task and not self.event_processor_task.done():
            owned_tasks.append(self.event_processor_task)
        if self.task_worker_task and not self.task_worker_task.done():
            owned_tasks.append(self.task_worker_task)

        if owned_tasks:
            logger.info(f"Cancelling {len(owned_tasks)} owned background tasks...")
            for task in owned_tasks:
                task.cancel()
            await asyncio.gather(*owned_tasks, return_exceptions=True)
            logger.info("Owned background tasks cancelled.")

        if self.telegram_service:
            await self.telegram_service.stop_polling()

        # Stop event processor if running
        if self.event_processor:
            await self.event_processor.stop()
            logger.info("Event processor stopped")

        # Uvicorn server task is awaited in start_services after shutdown_event.wait()

        if (
            self.fastapi_app
            and self.fastapi_app.state.processing_services
            and isinstance(self.fastapi_app.state.processing_services, dict)
        ):
            logger.info(
                f"Closing tool providers for {len(self.fastapi_app.state.processing_services)} services..."
            )
            for (
                profile_id,
                service_instance,
            ) in self.fastapi_app.state.processing_services.items():
                if (
                    hasattr(service_instance, "tools_provider")
                    and service_instance.tools_provider
                ):
                    try:
                        await service_instance.tools_provider.close()
                    except Exception as e:
                        logger.error(
                            f"Error closing tools_provider for profile '{profile_id}': {e}",
                            exc_info=True,
                        )
        elif (
            self.default_processing_service
            and self.default_processing_service.tools_provider
        ):
            logger.warning(
                "Processing services registry not found, closing default tools_provider."
            )
            await self.default_processing_service.tools_provider.close()

        if self.shared_httpx_client:
            await self.shared_httpx_client.aclose()
            logger.info("Shared httpx client closed.")

        # Close the error logging handler if it exists
        if self.error_logging_handler:
            self.error_logging_handler.close()
            logging.getLogger().removeHandler(self.error_logging_handler)
            logger.info("Error logging handler closed.")

        # Close database engine (only if we created it, not if it was injected)
        if self.database_engine and not self._injected_database_engine:
            await self.database_engine.dispose()
            logger.info("Database engine disposed.")
        elif self._injected_database_engine:
            logger.info(
                "Database engine was injected, not disposing (managed by caller)."
            )

        self._is_shutdown_complete = True
        logger.info("Assistant stop_services finished.")

    def is_shutdown_complete(self) -> bool:
        return self._is_shutdown_complete

    async def handle_reindex_document(
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        self,
        exec_context: ToolExecutionContext,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        payload: dict[str, Any],
    ) -> None:
        await handle_reindex_document(exec_context, payload)
