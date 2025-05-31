import asyncio
import copy
import logging
import os
from typing import Any

import httpx
import uvicorn

# Import Embedding interface/clients
import family_assistant.embeddings as embeddings

# Import the whole storage module for task queue functions etc.
from family_assistant import storage

# --- NEW: Import ContextProvider and its implementations ---
from family_assistant.context_providers import (
    CalendarContextProvider,
    KnownUsersContextProvider,
    NotesContextProvider,
    WeatherContextProvider,
)
from family_assistant.embeddings import (
    EmbeddingGenerator,
    LiteLLMEmbeddingGenerator,
)
from family_assistant.indexing.document_indexer import DocumentIndexer
from family_assistant.indexing.email_indexer import EmailIndexer
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

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.shutdown_event = asyncio.Event()
        self.new_task_event = asyncio.Event()

        self.shared_httpx_client: httpx.AsyncClient | None = None
        self.embedding_generator: EmbeddingGenerator | None = None
        self.processing_services_registry: dict[str, ProcessingService] = {}
        self.default_processing_service: ProcessingService | None = None
        self.scraper_instance: PlaywrightScraper | None = None
        self.document_indexer: DocumentIndexer | None = None
        self.email_indexer: EmailIndexer | None = None
        self.telegram_service: TelegramService | None = None
        self.task_worker_instance: TaskWorker | None = None
        self.uvicorn_server_task: asyncio.Task | None = None
        self._is_shutdown_complete = False

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
            await storage.init_vector_db(db_ctx)

        resolved_profiles = self.config.get("service_profiles", [])
        default_service_profile_id = self.config.get(
            "default_service_profile_id", "default_assistant"
        )

        available_doc_files = _scan_user_docs()
        formatted_doc_list_for_tool_desc = ", ".join(available_doc_files) or "None"
        base_local_tools_definition = copy.deepcopy(local_tools_definition)

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
            llm_client_for_profile: LLMInterface = LiteLLMClient(
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

            profile_local_implementations = {
                name: func
                for name, func in local_tool_implementations.items()
                if name in enabled_local_tool_names
            }
            local_provider_for_profile = LocalToolsProvider(
                definitions=profile_specific_local_definitions,
                implementations=profile_local_implementations,
                embedding_generator=self.embedding_generator,
                calendar_config=profile_proc_conf_dict["calendar_config"],
            )

            all_mcp_servers_config = self.config.get("mcp_config", {}).get(
                "mcpServers", {}
            )
            mcp_server_ids_from_config = profile_tools_conf_dict.get(
                "enable_mcp_server_ids"
            )
            enabled_mcp_server_ids = (
                set(all_mcp_servers_config.keys())
                if mcp_server_ids_from_config is None and all_mcp_servers_config
                else set(mcp_server_ids_from_config or [])
            )

            profile_mcp_servers_config = {
                sid: sconf
                for sid, sconf in all_mcp_servers_config.items()
                if sid in enabled_mcp_server_ids
            }
            mcp_timeout_seconds = profile_tools_conf_dict.get(
                "mcp_initialization_timeout_seconds", 60
            )
            mcp_provider_for_profile = MCPToolsProvider(
                mcp_server_configs=profile_mcp_servers_config,
                initialization_timeout_seconds=mcp_timeout_seconds,
            )

            composite_provider_for_profile = CompositeToolsProvider(
                providers=[local_provider_for_profile, mcp_provider_for_profile]
            )
            try:
                await composite_provider_for_profile.get_tool_definitions()
            except ValueError as provider_err:
                logger.critical(
                    f"Failed to initialize CompositeToolsProvider for profile '{profile_id}': {provider_err}",
                    exc_info=True,
                )
                raise SystemExit(
                    f"Tool provider init failed for '{profile_id}': {provider_err}"
                ) from provider_err

            profile_confirm_tools_set = set(
                profile_tools_conf_dict.get("confirm_tools", [])
            )
            confirming_provider_for_profile = ConfirmingToolsProvider(
                wrapped_provider=composite_provider_for_profile,
                tools_requiring_confirmation=profile_confirm_tools_set,
                calendar_config=profile_proc_conf_dict["calendar_config"],
            )
            await confirming_provider_for_profile.get_tool_definitions()

            notes_provider = NotesContextProvider(
                get_db_context_func=async_get_db_context_for_provider,
                prompts=profile_proc_conf_dict["prompts"],
            )
            calendar_provider = CalendarContextProvider(
                calendar_config=profile_proc_conf_dict["calendar_config"],
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

            service_config = ProcessingServiceConfig(
                prompts=profile_proc_conf_dict["prompts"],
                calendar_config=profile_proc_conf_dict["calendar_config"],
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
        fastapi_app.state.tools_provider = (
            self.default_processing_service.tools_provider
        )
        fastapi_app.state.tool_definitions = (
            await self.default_processing_service.tools_provider.get_tool_definitions()
        )
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
        logger.info("DocumentIndexer and EmailIndexer initialized.")

    async def start_services(self) -> None:
        """Starts all long-running services and waits for shutdown."""
        if not self.default_processing_service or not self.embedding_generator:
            raise RuntimeError("Dependencies not set up before starting services.")

        self.telegram_service = TelegramService(
            telegram_token=self.config["telegram_token"],
            allowed_user_ids=self.config["allowed_user_ids"],
            developer_chat_id=self.config["developer_chat_id"],
            processing_service=self.default_processing_service,
            processing_services_registry=self.processing_services_registry,
            app_config=self.config,
            get_db_context_func=get_db_context,  # Direct function, not wrapped one
            new_task_event=self.new_task_event,
        )
        await self.telegram_service.start_polling()
        fastapi_app.state.telegram_service = self.telegram_service
        logger.info("TelegramService started and stored in FastAPI app state.")

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
            new_task_event=self.new_task_event,
            calendar_config=default_profile_conf["processing_config"][
                "calendar_config"
            ],
            timezone_str=default_profile_conf["processing_config"]["timezone"],
            embedding_generator=self.embedding_generator,
            shutdown_event=self.shutdown_event,  # Pass the assistant's shutdown_event
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
        self.task_worker_instance.register_task_handler(
            "llm_callback", handle_llm_callback
        )
        self.task_worker_instance.register_task_handler(
            "embed_and_store_batch", handle_embed_and_store_batch
        )
        logger.info(
            f"Registered task handlers for worker {self.task_worker_instance.worker_id}"
        )
        asyncio.create_task(
            self.task_worker_instance.run()
        )  # Removed new_task_event from run()

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

        self._is_shutdown_complete = True
        logger.info("Assistant stop_services finished.")

    def is_shutdown_complete(self) -> bool:
        return self._is_shutdown_complete
