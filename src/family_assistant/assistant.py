import asyncio
import copy
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx
import uvicorn

# Import Embedding interface/clients
import family_assistant.embeddings as embeddings

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
from family_assistant.llm import (
    LiteLLMClient,
    LLMInterface,
)
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage import init_db
from family_assistant.storage.context import (
    DatabaseContext,
    get_db_context,
)
from family_assistant.task_worker import (
    TaskWorker,
    handle_llm_callback,
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
from family_assistant.tools.types import ToolExecutionContext
from family_assistant.utils.scraping import PlaywrightScraper
from family_assistant.web.app_creator import app as fastapi_app

from .telegram_bot import TelegramService

logger = logging.getLogger(__name__)


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


# --- Wrapper Functions for Type Compatibility ---
# These wrappers might be needed by task handlers if they are registered from here
# or if the Assistant class sets up the task worker directly.
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
        return
    await original_handle_log_message(exec_context.db_context, payload)


class Assistant:
    """
    Orchestrates the Family Assistant application's lifecycle, including
    dependency setup, service initialization, and graceful shutdown.
    """

    def __init__(
        self,
        config: dict[str, Any],
        llm_client_overrides: dict[str, LLMInterface] | None = None,
    ) -> None:
        self.config = config
        self.shutdown_event = asyncio.Event()
        self.llm_client_overrides = (
            llm_client_overrides if llm_client_overrides is not None else {}
        )

        self.shared_httpx_client: httpx.AsyncClient | None = None
        self.embedding_generator: EmbeddingGenerator | None = None
        self.processing_services_registry: dict[str, ProcessingService] = {}
        self.default_processing_service: ProcessingService | None = None
        self.scraper_instance: PlaywrightScraper | None = None
        self.document_indexer: DocumentIndexer | None = None
        self.email_indexer: EmailIndexer | None = None
        self.notes_indexer: NotesIndexer | None = None
        self.telegram_service: TelegramService | None = None
        self.task_worker_instance: TaskWorker | None = None
        self.uvicorn_server_task: asyncio.Task | None = None
        self._is_shutdown_complete = False

        # Event system
        self.event_processor: EventProcessor | None = None
        self.home_assistant_clients: dict[str, Any] = {}  # profile_id -> HA client

        # Logging handler
        self.error_logging_handler = None

    async def setup_dependencies(self) -> None:
        """Initializes and wires up all core application components."""
        logger.info(f"Using model: {self.config['model']}")

        self.shared_httpx_client = httpx.AsyncClient()
        logger.info("Shared httpx.AsyncClient created.")

        if not self.config.get("telegram_token"):
            raise ValueError("Telegram Bot Token is missing.")

        selected_model = self.config.get("model", "")
        if selected_model.startswith("gemini/"):
            if not os.getenv("GEMINI_API_KEY"):
                raise ValueError("Gemini API Key is missing (GEMINI_API_KEY env var).")
            logger.info("Gemini model selected. Using GEMINI_API_KEY from environment.")
        elif selected_model.startswith("openrouter/"):
            if not self.config.get("openrouter_api_key"):
                raise ValueError("OpenRouter API Key is missing.")
            if self.config.get(
                "openrouter_api_key"
            ):  # Redundant check, but for clarity
                os.environ["OPENROUTER_API_KEY"] = self.config["openrouter_api_key"]
            logger.info(
                "OpenRouter model selected. OPENROUTER_API_KEY set for LiteLLM."
            )
        else:
            logger.warning(
                f"No specific API key validation for model: {selected_model}."
            )

        embedding_model_name = self.config["embedding_model"]
        embedding_dimensions = self.config["embedding_dimensions"]
        if embedding_model_name == "mock-deterministic-embedder":
            self.embedding_generator = embeddings.MockEmbeddingGenerator(
                model_name=embedding_model_name,
                dimensions=embedding_dimensions,
                default_embedding_behavior="generate",
            )
        elif embedding_model_name.startswith("/") or embedding_model_name in [
            "all-MiniLM-L6-v2",
            "other-local-model-name",
        ]:
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
        fastapi_app.state.embedding_generator = self.embedding_generator

        await init_db()
        async with get_db_context() as db_ctx:
            await db_ctx.init_vector_db()

        # Setup error logging to database if enabled
        error_logging_config = self.config.get("logging", {}).get("database_errors", {})
        # Also check environment variable to disable for testing
        if error_logging_config.get("enabled", True) and not os.environ.get(
            "FAMILY_ASSISTANT_DISABLE_DB_ERROR_LOGGING"
        ):
            from family_assistant.utils.logging_handler import setup_error_logging

            self.error_logging_handler = setup_error_logging(get_db_context)
            logger.info("Database error logging handler initialized")

        resolved_profiles = self.config.get("service_profiles", [])
        default_service_profile_id = self.config.get(
            "default_service_profile_id", "default_assistant"
        )

        available_doc_files = _scan_user_docs()
        formatted_doc_list_for_tool_desc = ", ".join(available_doc_files) or "None"
        base_local_tools_definition = copy.deepcopy(local_tools_definition)

        # Prepare the string listing available service profiles and their descriptions
        profile_descriptions_list = []
        for profile_config_item in resolved_profiles:
            profile_id_item = profile_config_item.get("id", "Unknown ID")
            description_item = profile_config_item.get(
                "description", "No description available."
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
            calendar_config=self.config.get(
                "calendar_config"
            ),  # Top-level calendar config
        )

        # Create root MCP provider with ALL configured servers
        all_mcp_servers_config = self.config.get("mcp_config", {}).get("mcpServers", {})
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
        fastapi_app.state.tools_provider = self.root_tools_provider
        fastapi_app.state.tool_definitions = (
            await self.root_tools_provider.get_tool_definitions()
        )
        logger.info(
            f"Root ToolsProvider initialized with {len(fastapi_app.state.tool_definitions)} tools"
        )

        for profile_conf in resolved_profiles:
            profile_id = profile_conf["id"]
            logger.info(
                f"Initializing ProcessingService for profile ID: '{profile_id}'"
            )
            profile_proc_conf_dict = profile_conf["processing_config"]
            profile_tools_conf_dict = profile_conf["tools_config"]
            profile_chat_id_map = profile_conf.get("chat_id_to_name_map", {})

            profile_llm_model = profile_proc_conf_dict.get(
                "llm_model", self.config["model"]
            )

            if profile_id in self.llm_client_overrides:
                llm_client_for_profile = self.llm_client_overrides[profile_id]
                logger.info(
                    f"Profile '{profile_id}' using overridden LLM client: {type(llm_client_for_profile).__name__}"
                )
            else:
                llm_client_for_profile = LiteLLMClient(
                    model=profile_llm_model,
                    model_parameters=self.config.get("llm_parameters", {}),
                )

            local_tools_list_from_config = profile_tools_conf_dict.get(
                "enable_local_tools"
            )
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
            mcp_server_ids_from_config = profile_tools_conf_dict.get(
                "enable_mcp_server_ids"
            )

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
            profile_confirm_tools_set = set(
                profile_tools_conf_dict.get("confirm_tools", [])
            )
            confirming_provider_for_profile = ConfirmingToolsProvider(
                wrapped_provider=filtered_provider,
                tools_requiring_confirmation=profile_confirm_tools_set,
            )
            await confirming_provider_for_profile.get_tool_definitions()

            notes_provider = NotesContextProvider(
                get_db_context_func=async_get_db_context_for_provider,
                prompts=profile_proc_conf_dict["prompts"],
            )
            calendar_provider = CalendarContextProvider(
                calendar_config=self.config.get(
                    "calendar_config", {}
                ),  # Use top-level calendar config with empty dict fallback
                timezone_str=profile_proc_conf_dict["timezone"],
                prompts=profile_proc_conf_dict["prompts"],
            )
            known_users_provider = KnownUsersContextProvider(
                chat_id_to_name_map=profile_chat_id_map,
                prompts=profile_proc_conf_dict["prompts"],
            )
            context_providers = [
                notes_provider,
                calendar_provider,
                known_users_provider,
            ]

            willyweather_api_key = self.config.get("willyweather_api_key")
            willyweather_location_id = self.config.get("willyweather_location_id")
            if (
                willyweather_api_key
                and willyweather_location_id
                and self.shared_httpx_client
            ):
                weather_provider = WeatherContextProvider(
                    location_id=willyweather_location_id,
                    api_key=willyweather_api_key,
                    prompts=profile_proc_conf_dict["prompts"],
                    timezone_str=profile_proc_conf_dict["timezone"],
                    httpx_client=self.shared_httpx_client,
                )
                context_providers.append(weather_provider)

            # --- Home Assistant Context Provider ---
            ha_api_url = profile_proc_conf_dict.get("home_assistant_api_url")
            ha_token = profile_proc_conf_dict.get("home_assistant_token")
            ha_template = profile_proc_conf_dict.get("home_assistant_context_template")
            ha_verify_ssl = profile_proc_conf_dict.get(
                "home_assistant_verify_ssl", True
            )

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
                                prompts=profile_proc_conf_dict["prompts"],
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
                prompts=profile_proc_conf_dict["prompts"],
                timezone_str=profile_proc_conf_dict["timezone"],
                max_history_messages=profile_proc_conf_dict["max_history_messages"],
                history_max_age_hours=profile_proc_conf_dict["history_max_age_hours"],
                tools_config=profile_tools_conf_dict,
                delegation_security_level=profile_proc_conf_dict.get(
                    "delegation_security_level", "confirm"
                ),
                id=profile_id,
            )

            processing_service_instance = ProcessingService(
                llm_client=llm_client_for_profile,
                tools_provider=confirming_provider_for_profile,
                service_config=service_config,
                context_providers=context_providers,
                server_url=self.config["server_url"],
                app_config=self.config,
            )
            # Set the home_assistant_client if available for this profile
            if profile_id in self.home_assistant_clients:
                processing_service_instance.home_assistant_client = (
                    self.home_assistant_clients[profile_id]
                )
            self.processing_services_registry[profile_id] = processing_service_instance

        if not self.processing_services_registry:
            logger.critical("No processing service profiles initialized.")
            raise SystemExit("No processing service profiles initialized.")

        for service_instance in self.processing_services_registry.values():
            service_instance.set_processing_services_registry(
                self.processing_services_registry
            )

        fastapi_app.state.processing_services = self.processing_services_registry

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

        fastapi_app.state.processing_service = self.default_processing_service
        fastapi_app.state.llm_client = self.default_processing_service.llm_client
        # Note: tools_provider and tool_definitions are already set to root provider above
        logger.info(
            f"Default processing service set to profile ID: '{default_service_profile_id}'."
        )

        self.scraper_instance = PlaywrightScraper()
        fastapi_app.state.scraper = self.scraper_instance

        pipeline_config = self.config.get("indexing_pipeline_config", {})
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

        self.telegram_service = TelegramService(
            telegram_token=self.config["telegram_token"],
            allowed_user_ids=self.config["allowed_user_ids"],
            developer_chat_id=self.config["developer_chat_id"],
            processing_service=self.default_processing_service,
            processing_services_registry=self.processing_services_registry,
            app_config=self.config,
            get_db_context_func=get_db_context,
            # use_batching argument removed
        )
        fastapi_app.state.telegram_service = self.telegram_service
        logger.info(
            "TelegramService instantiated and stored in FastAPI app state during setup_dependencies."
        )

        # Initialize event system if enabled
        event_config = self.config.get("event_system", {})
        if event_config.get("enabled", False):
            event_sources = {}  # Dict, not list

            # Create Home Assistant event sources for unique HA instances
            if (
                event_config.get("sources", {})
                .get("home_assistant", {})
                .get("enabled", False)
            ):
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
                storage_config = event_config.get("storage", {})
                sample_interval_hours = storage_config.get("sample_interval_hours", 1.0)

                self.event_processor = EventProcessor(
                    sources=event_sources,
                    sample_interval_hours=sample_interval_hours,
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
        if not self.telegram_service:
            raise RuntimeError(
                "TelegramService not initialized before starting services."
            )

        await self.telegram_service.start_polling()
        logger.info("TelegramService polling started.")

        uvicorn_config = uvicorn.Config(
            fastapi_app, host="0.0.0.0", port=8000, log_level="info"
        )
        server = uvicorn.Server(uvicorn_config)
        self.uvicorn_server_task = asyncio.create_task(server.serve())
        logger.info("Web server running on http://0.0.0.0:8000")

        default_profile_conf = next(
            p
            for p in self.config["service_profiles"]
            if p["id"] == self.default_processing_service.service_config.id
        )

        self.task_worker_instance = TaskWorker(
            processing_service=self.default_processing_service,
            chat_interface=self.telegram_service.chat_interface,
            calendar_config=self.config.get(
                "calendar_config", {}
            ),  # Use top-level calendar config with empty dict fallback
            timezone_str=default_profile_conf["processing_config"]["timezone"],
            embedding_generator=self.embedding_generator,
            # shutdown_event is likely handled internally by TaskWorker or passed differently
            indexing_source=getattr(
                self, "indexing_source", None
            ),  # Pass indexing source if available
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
        logger.info(
            f"Registered task handlers for worker {self.task_worker_instance.worker_id}"
        )
        asyncio.create_task(self.task_worker_instance.run())

        # Start event processor if initialized
        if self.event_processor:
            asyncio.create_task(self.event_processor.start())
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
        import zoneinfo
        from datetime import timedelta

        async with get_db_context() as db_ctx:
            # Get the timezone from the default profile
            if not self.default_processing_service:
                logger.error(
                    "Default processing service not available for system tasks setup"
                )
                return

            default_profile_conf = next(
                p
                for p in self.config["service_profiles"]
                if p["id"] == self.default_processing_service.service_config.id
            )
            timezone_str = default_profile_conf["processing_config"]["timezone"]
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
            next_3am_utc = next_3am_local.astimezone(timezone.utc)

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
                    self.config.get("logging", {})
                    .get("database_errors", {})
                    .get("retention_days", 30)
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

    async def stop_services(self) -> None:
        """Gracefully stops all managed services."""
        if self._is_shutdown_complete:
            logger.info("stop_services already completed.")
            return

        logger.info("Assistant stop_services called.")
        # Ensure shutdown_event is set, in case stop_services is called directly
        if not self.shutdown_event.is_set():
            self.shutdown_event.set()

        # Cancel outstanding asyncio tasks
        loop = asyncio.get_running_loop()
        tasks = [
            t for t in asyncio.all_tasks(loop=loop) if t is not asyncio.current_task()
        ]
        if tasks:
            logger.info(f"Cancelling {len(tasks)} outstanding tasks...")
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.info("Outstanding tasks cancelled.")

        if self.telegram_service:
            await self.telegram_service.stop_polling()

        # Stop event processor if running
        if self.event_processor:
            await self.event_processor.stop()
            logger.info("Event processor stopped")

        # Uvicorn server task is awaited in start_services after shutdown_event.wait()

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

        self._is_shutdown_complete = True
        logger.info("Assistant stop_services finished.")

    def is_shutdown_complete(self) -> bool:
        return self._is_shutdown_complete
