from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import os
import sys
from asyncio import subprocess as asyncio_subprocess
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import httpx
import uvicorn

# Import Embedding interface/clients
from family_assistant import embeddings
from family_assistant.config_models import (
    DEFAULT_REMOTE_MAX_ASYNC_SECONDS,
    AppConfig,  # Used at runtime
)
from family_assistant.config_models import (  # Used at runtime
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
from family_assistant.email_intake.actions import (
    EMAIL_INTAKE_ACTION_TASK_TYPE,
    handle_email_intake_action,
)
from family_assistant.email_intake.outbound import (
    EmailChatInterface,
    MailgunOutboundEmailClient,
)
from family_assistant.embeddings import (
    EmbeddingGenerator,
    GoogleEmbeddingGenerator,
    OpenAIEmbeddingGenerator,
)
from family_assistant.events.home_assistant_source import HomeAssistantSource
from family_assistant.events.indexing_source import IndexingSource
from family_assistant.events.processor import EventProcessor
from family_assistant.events.webhook_source import WebhookEventSource
from family_assistant.home_assistant_shared import create_home_assistant_client
from family_assistant.indexing.document_indexer import DocumentIndexer
from family_assistant.indexing.email_indexer import EmailIndexer
from family_assistant.indexing.message_history_indexer import (
    enqueue_message_history_backfill_task,
    handle_index_message_history_batch,
)
from family_assistant.indexing.notes_indexer import NotesIndexer
from family_assistant.indexing.tasks import handle_embed_and_store_batch
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.llm.providers.google_genai_client import is_deep_research_model
from family_assistant.paths import PACKAGE_ROOT
from family_assistant.processing import (
    DelegatableService,
    ProcessingService,
    ProcessingServiceConfig,
)
from family_assistant.processing.deep_research_service import (
    DeepResearchProcessingService,
)
from family_assistant.security.taint import TaintMetadata, merge_taint_policy_config
from family_assistant.services.api_backend import HttpApiBackend
from family_assistant.services.apns import APNsService, load_apns_auth_key
from family_assistant.services.confirmation_service import (
    CONFIRMATION_TOOL_EXECUTION_TASK_TYPE,
    ConfirmationService,
)
from family_assistant.services.confirmation_waiters import (
    ConfirmationResultWaiterRegistry,
)
from family_assistant.services.credential_encryption import CredentialEncryption
from family_assistant.services.google_provider import GOOGLE_PROVIDER
from family_assistant.services.notification_dispatcher import NotificationDispatcher
from family_assistant.services.oauth_credentials import OAuthCredentialResolver
from family_assistant.services.oauth_integration_state import (
    OAuthIntegrationState,
    evaluate_oauth_integration_state,
    filter_oauth_tool_registrations,
)
from family_assistant.services.push_notification import PushNotificationService
from family_assistant.services.user_identity import UserIdentityResolver
from family_assistant.services.worker_backend import get_worker_backend
from family_assistant.skills import NoteRegistry, load_skills_from_directory
from family_assistant.storage import init_db
from family_assistant.storage.base import create_engine_with_sqlite_optimizations
from family_assistant.storage.context import (
    DatabaseContext,
    get_db_context,
    set_engine_history_taint_epoch,
)
from family_assistant.task_worker import (
    SCHEDULE_AUTOMATION_ADVANCE_TASK_TYPE,
    ReindexDocumentPayload,
    TaskWorker,
    handle_completed_automation_cleanup,
    handle_confirmation_tool_execution,
    handle_llm_callback,
    handle_reindex_document,
    handle_script_execution,
    handle_system_error_log_cleanup,
    handle_system_event_cleanup,
    handle_worker_task_cleanup,
)
from family_assistant.task_worker import (
    handle_log_message as original_handle_log_message,
)
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS as local_tool_implementations,
)
from family_assistant.tools import (
    LOCAL_TOOL_METADATA_BY_NAME as local_tool_metadata_by_name,
)
from family_assistant.tools import (
    TOOLS_DEFINITION as local_tools_definition,
)
from family_assistant.tools import (
    CompositeToolsProvider,
    LocalToolsProvider,
    MCPServerConfig,
    MCPToolsProvider,
    OnDemandToolsView,
    PolicyEnforcingToolsProvider,
    PolicyEngine,
    PolicyRule,
    TaintTrackingToolsProvider,
    ToolMatcher,
    ToolPolicyConfig,
    ToolPolicyDecision,
    _scan_user_docs,
    build_local_tool_registrations,
)
from family_assistant.tools.google_data import GOOGLE_TOOL_REQUIRED_SCOPES
from family_assistant.tools.worker import reconcile_stale_tasks
from family_assistant.utils.logging_handler import setup_error_logging
from family_assistant.utils.scraping import PlaywrightScraper
from family_assistant.web.app_creator import configure_app_auth, create_app
from family_assistant.web.auth import AUTH_ENABLED
from family_assistant.web.web_confirmation_ui_manager import WebConfirmationUIManager

from .telegram.service import TelegramService

if TYPE_CHECKING:
    import socket

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.config_models import ServiceProfile
    from family_assistant.llm import LLMInterface
    from family_assistant.services.attachment_registry import AttachmentRegistry
    from family_assistant.storage.types import EventConditionEvaluatorConfig
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


def _build_profile_policy_engine(
    profile_id: str,
    profile_tools_policy: ToolPolicyConfig | None,
    operator_tools_policy: ToolPolicyConfig | None,
    global_tools_policy: ToolPolicyConfig | None = None,
) -> PolicyEngine:
    """Build a policy engine for a profile from explicit policy config.

    Automatically injects a synthetic self-delegation allow rule at the
    ``profile`` layer so that every profile can delegate to itself without
    confirmation.  Self-delegation is never a privilege escalation.

    Rules from ``global_tools_policy`` are injected at the same ``profile``
    layer so they apply to every profile regardless of the profile's own
    ``tools_policy`` (which otherwise replaces the shipped defaults wholesale).
    Operator policy still takes precedence over global rules.
    """
    if profile_tools_policy is None:
        msg = (
            f"Profile '{profile_id}' is missing tools_policy. "
            "Every runtime profile must define explicit tools_policy."
        )
        raise ValueError(msg)

    synthetic_rules: list[PolicyRule] = [
        PolicyRule(
            match=ToolMatcher(
                names=["delegate_to_service"],
                argument_equals={"target_service_id": profile_id},
            ),
            decision=ToolPolicyDecision.ALLOW,
            priority=50,
            description=f"Allow self-delegation for profile '{profile_id}'",
        ),
    ]
    if global_tools_policy is not None:
        synthetic_rules.extend(global_tools_policy.rules)

    synthetic_policy = ToolPolicyConfig(rules=synthetic_rules)

    return PolicyEngine.from_layers(
        defaults=profile_tools_policy,
        profile=synthetic_policy,
        operator=operator_tools_policy,
    )


class NullChatInterface:
    """A null chat interface for when Telegram service is not configured."""

    async def send_message(
        self,
        conversation_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_to_interface_id: str | None = None,
        attachment_ids: list[str] | None = None,
        on_behalf_of_user_id: str | None = None,
        taint_metadata: TaintMetadata | None = None,
    ) -> str | None:
        """Does nothing, returns None."""
        _ = taint_metadata
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
    # ast-grep-ignore: no-dict-any - ToolExecutionContext type alias requires this signature
    exec_context: ToolExecutionContext,
    # ast-grep-ignore: no-dict-any - task payload has varying keys per task type
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
        # Dedicated engines created for pool workers (one per worker) so each has
        # its own DB connection and a worker parked inside a transaction does not
        # block its siblings. Disposed on shutdown.
        self.worker_engines: list[AsyncEngine] = []

        # Initialize all instance attributes
        self.fastapi_app: FastAPI | None = None
        self.shared_httpx_client: httpx.AsyncClient | None = None
        self.embedding_generator: EmbeddingGenerator | None = None
        self.processing_services_registry: dict[str, DelegatableService] = {}
        self.a2a_cancel_events: dict[str, asyncio.Event] = {}
        self.a2a_background_tasks: dict[str, asyncio.Task[None]] = {}
        self.default_processing_service: ProcessingService | None = None
        self.scraper_instance: PlaywrightScraper | None = None
        self.attachment_registry: AttachmentRegistry | None = None
        self.document_indexer: DocumentIndexer | None = None
        self.email_indexer: EmailIndexer | None = None
        self.email_chat_interface: EmailChatInterface | None = None
        self.notes_indexer: NotesIndexer | None = None
        self.telegram_service: TelegramService | None = None
        self.push_notification_service: PushNotificationService | None = None
        self.apns_service: APNsService | None = None
        self.notification_dispatcher: NotificationDispatcher | None = None
        self.confirmation_service: ConfirmationService | None = None
        self.confirmation_result_waiters: ConfirmationResultWaiterRegistry | None = None
        self.oauth_integration_states: dict[str, OAuthIntegrationState] = {}
        self.credential_resolvers: dict[str, OAuthCredentialResolver] = {}
        self.api_backend: HttpApiBackend | None = None
        # Pool of in-process TaskWorker instances and their run() tasks, kept
        # index-aligned so the health monitor can restart an individual worker.
        self.task_workers: list[TaskWorker] = []
        self.task_worker_tasks: list[asyncio.Task] = []
        self.uvicorn_server_task: asyncio.Task | None = None
        self.health_monitor_task: asyncio.Task | None = None  # Track health monitor
        self.event_processor_task: asyncio.Task | None = None  # Track event processor
        self._is_shutdown_complete = False

        # Event system
        self.event_processor: EventProcessor | None = None
        # ast-grep-ignore: no-dict-any - maps profile IDs to heterogeneous HA client objects
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
        except Exception as e:
            logger.warning(f"Could not check/install Playwright browsers: {e}")

    def _setup_notifications(self) -> None:
        """Initialize Web Push and iOS APNs services and the fan-out dispatcher."""
        assert self.fastapi_app is not None
        self.push_notification_service = PushNotificationService(
            vapid_private_key=self.config.pwa_config.vapid_private_key,
            vapid_contact_email=self.config.pwa_config.vapid_contact_email,
        )

        apns_conf = self.config.apns
        apns_auth_key = load_apns_auth_key(
            auth_key=apns_conf.auth_key,
            auth_key_path=apns_conf.auth_key_path,
        )
        self.apns_service = APNsService(
            team_id=apns_conf.team_id,
            key_id=apns_conf.key_id,
            auth_key=apns_auth_key,
            bundle_id=apns_conf.bundle_id,
            use_sandbox=apns_conf.use_sandbox,
        )

        self.notification_dispatcher = NotificationDispatcher(
            web_push=self.push_notification_service,
            apns=self.apns_service,
        )

        self.fastapi_app.state.push_notification_service = (
            self.push_notification_service
        )
        self.fastapi_app.state.notification_dispatcher = self.notification_dispatcher
        logger.info(
            "Notifications initialized (web_push=%s, apns=%s)",
            self.push_notification_service.enabled,
            self.apns_service.enabled,
        )

    def _log_google_integration_state(self, state: OAuthIntegrationState) -> None:
        """Log the resolved Google integration state at the right severity.

        A disabled integration that the operator *tried* to configure is a
        root-logger error (so it lands in the DB error log); a fully unconfigured
        one is only debug. A waiver is a warning stating the explicit risk
        acceptance.
        """
        if state.enabled:
            if state.taint_enforcement_waived:
                logger.warning(
                    "Google integration ENABLED with taint enforcement WAIVED "
                    "(google_integration.require_taint_enforcement: false): the "
                    "Gmail/Drive tools run without the taint floor. This is a "
                    "deliberate, logged risk acceptance. Enabled tools: %s",
                    sorted(state.enabled_tool_names),
                )
            else:
                logger.info(
                    "Google integration enabled. Enabled tools: %s",
                    sorted(state.enabled_tool_names),
                )
            return

        if self._any_google_config_field_set():
            logger.error("Google integration disabled: %s", state.reason)
        else:
            logger.debug("Google integration not configured: %s", state.reason)

    def _any_google_config_field_set(self) -> bool:
        """Return True if the operator set any Google config field."""
        integration = self.config.google_integration
        return bool(
            integration.oauth_client_id
            or integration.oauth_client_secret
            or integration.credential_encryption_key
        )

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
        user_identity_resolver = UserIdentityResolver(self.config)
        self.fastapi_app.state.user_identity_resolver = user_identity_resolver
        logger.info("Stored configuration in FastAPI app state.")

        # Store shutdown event for SSE and other async endpoints
        self.fastapi_app.state.shutdown_event = self.shutdown_event

        # Initialize chat_interfaces registry for cross-interface messaging
        self.fastapi_app.state.chat_interfaces = {}
        logger.info("Chat interfaces registry initialized")
        self.fastapi_app.state.confirmation_ui_managers = {}
        logger.info("Confirmation UI manager registry initialized")

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
            os.environ["OPENROUTER_API_KEY"] = self.config.openrouter_api_key
            logger.info("OpenRouter model selected. OPENROUTER_API_KEY set.")
        else:
            logger.warning(
                f"No specific API key validation for model: {selected_model}."
            )

        embedding_model_name = self.config.embedding_model
        embedding_dimensions = self.config.embedding_dimensions
        embedding_provider = self.config.embedding_provider
        if embedding_provider == "openai":
            api_key = (
                self.config.embedding_api_key
                or self.config.openai_api_key
                or os.getenv("OPENAI_API_KEY")
            )
            if not api_key and not self.config.embedding_base_url:
                raise ValueError(
                    "embedding_provider='openai' requires an API key "
                    "(embedding_api_key / openai_api_key / OPENAI_API_KEY) "
                    "or a custom embedding_base_url."
                )
            # Only forward the optional `dimensions` request parameter when the
            # operator explicitly configured it. Models such as
            # text-embedding-ada-002 (and some OpenAI-compatible servers) reject
            # the field, and the default value would otherwise always be sent.
            explicit_dimensions = (
                embedding_dimensions
                if "embedding_dimensions" in self.config.model_fields_set
                else None
            )
            self.embedding_generator = OpenAIEmbeddingGenerator(
                model=embedding_model_name,
                api_key=api_key,
                base_url=self.config.embedding_base_url,
                dimensions=explicit_dimensions,
            )
        elif embedding_model_name == "mock-deterministic-embedder":
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
        elif embedding_model_name.startswith(("gemini/", "gemini-")):
            canonical_name = embedding_model_name
            if not canonical_name.startswith("gemini/"):
                canonical_name = f"gemini/{canonical_name}"
            if canonical_name == "gemini/":
                raise ValueError("Embedding model name cannot be just 'gemini/'.")
            self.embedding_generator = GoogleEmbeddingGenerator(
                model=canonical_name,
                dimensions=embedding_dimensions,
            )
        else:
            raise ValueError(
                f"Unsupported embedding model: '{embedding_model_name}'. "
                f"Supported formats: 'gemini/<model>' for Google Gemini models "
                f"(e.g., 'gemini/gemini-embedding-001'), "
                f"'mock-deterministic-embedder' for testing, "
                f"or a local model path starting with '/'."
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

        # Attach the deployment history taint epoch to the engine so every
        # DatabaseContext (web, telegram, task worker, scripts) applies the same
        # read-time amnesty when materializing message-history taint metadata.
        set_engine_history_taint_epoch(
            self.database_engine,
            self.config.taint_policy.history_taint_epoch,
        )

        # Store engine in FastAPI app state for web dependencies
        self.fastapi_app.state.database_engine = self.database_engine

        # Initialize notification channels (Web Push + iOS APNs) and the dispatcher that fans
        # out to all configured channels. Built before the confirmation service so it can be
        # injected as a dependency.
        self._setup_notifications()

        database_engine = self.database_engine
        assert database_engine is not None
        self.confirmation_service = ConfirmationService(
            db_context_factory=lambda: get_db_context(database_engine),
            notifier=self.notification_dispatcher,
        )
        self.confirmation_result_waiters = ConfirmationResultWaiterRegistry()
        self.fastapi_app.state.confirmation_service = self.confirmation_service
        self.fastapi_app.state.confirmation_result_waiters = (
            self.confirmation_result_waiters
        )
        # Register a durable web confirmation manager so confirmations work for
        # background runs (e.g. async profile delegation) on the web interface,
        # not just inside a live streaming turn.
        self.fastapi_app.state.confirmation_ui_managers["web"] = (
            WebConfirmationUIManager(
                confirmation_service=self.confirmation_service,
                confirmation_result_waiters=self.confirmation_result_waiters,
                stream_hub=getattr(
                    self.fastapi_app.state, "conversation_stream_hub", None
                ),
            )
        )
        outbound_email_client = None
        email_config = self.config.email_intake
        if (
            email_config.outbound_mailgun_api_key
            and email_config.outbound_mailgun_domain
            and self.shared_httpx_client is not None
        ):
            outbound_email_client = MailgunOutboundEmailClient(
                api_key=email_config.outbound_mailgun_api_key,
                domain=email_config.outbound_mailgun_domain,
                http_client=self.shared_httpx_client,
                timeout_seconds=email_config.outbound_timeout_seconds,
            )
        self.email_chat_interface = EmailChatInterface(
            database_engine=database_engine,
            outbound_client=outbound_email_client,
            config=email_config,
            user_identity_resolver=user_identity_resolver,
        )
        self.fastapi_app.state.chat_interfaces["email"] = self.email_chat_interface

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
            AttachmentRegistryConfig,
        )

        # Include the mailbox base so legacy email attachments with a
        # relative ``storage_path`` resolve against a stable directory
        # instead of the worker process's cwd. ``AppConfig`` normalizes
        # ``attachment_storage_path`` to an absolute path at load time
        # (see the field validator), so by the time it reaches us here
        # it's already stable across restarts regardless of cwd.
        registry_config_payload = cast(
            "AttachmentRegistryConfig", attachment_config.model_dump()
        )
        if self.config.attachment_storage_path:
            registry_config_payload["email_attachment_base_path"] = (
                self.config.attachment_storage_path
            )
        self.attachment_registry = AttachmentRegistry(
            storage_path=attachment_storage_path,
            db_engine=self.database_engine,
            config=registry_config_payload,
        )

        # Store in FastAPI app state for web access
        self.fastapi_app.state.attachment_registry = self.attachment_registry
        logger.info(
            f"AttachmentRegistry initialized with path: {attachment_storage_path}"
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

        # Format the doc tool description with the list of available user docs.
        # The delegate_to_service profile catalog is no longer injected into the
        # tool schema; it is appended to each delegate-capable profile's system
        # prompt instead (see ProcessingService.delegation_catalog_addition).
        for tool_def_template in base_local_tools_definition:
            tool_name = tool_def_template.get("function", {}).get("name")
            if tool_name == "get_user_documentation_content":
                try:
                    tool_def_template["function"]["description"] = tool_def_template[
                        "function"
                    ]["description"].format(
                        available_doc_files=formatted_doc_list_for_tool_desc
                    )
                except KeyError as e:
                    logger.error(
                        "Failed to format doc tool description during assistant setup: %s",
                        e,
                    )
                break

        # Update the spawn_worker tool's agent enum from config
        available_agents = self.config.ai_worker_config.available_agents
        for tool_def_template in base_local_tools_definition:
            if tool_def_template.get("function", {}).get("name") == "spawn_worker":
                agent_param = (
                    tool_def_template["function"]
                    .get("parameters", {})
                    .get("properties", {})
                    .get("agent")
                )
                if agent_param:
                    agent_param["enum"] = available_agents
                    logger.debug(
                        f"Updated spawn_worker agent enum to {available_agents}"
                    )
                break

        # Resolve Google (Gmail/Drive) integration enablement ONCE. The state is
        # the single source of truth consumed by the tool-gating below, the status
        # endpoint, and the connect-flow 409s. Disabled Google tools are dropped
        # from the shared root definitions so no profile (nor UI/API) can advertise
        # a tool whose credentials cannot serve it.
        google_integration_state = evaluate_oauth_integration_state(
            GOOGLE_PROVIDER,
            self.config,
            auth_enabled=AUTH_ENABLED,
            tool_required_scopes=GOOGLE_TOOL_REQUIRED_SCOPES,
        )
        self.oauth_integration_states[GOOGLE_PROVIDER.name] = google_integration_state
        self.fastapi_app.state.oauth_integration_states = self.oauth_integration_states
        self.fastapi_app.state.oauth_http_client = self.shared_httpx_client
        self._log_google_integration_state(google_integration_state)
        if google_integration_state.enabled:
            encryption = CredentialEncryption(
                self.config.google_integration.credential_encryption_key
            )
            self.credential_resolvers[GOOGLE_PROVIDER.name] = OAuthCredentialResolver(
                GOOGLE_PROVIDER,
                self.config.google_integration,
                encryption,
                self.shared_httpx_client,
                self.notification_dispatcher,
            )
            self.api_backend = HttpApiBackend(self.shared_httpx_client)

        # Create root providers with ALL tools for UI/API access
        logger.info("Creating root ToolsProvider with all available tools")

        # Create root local provider with ALL tools, then drop Google tools the
        # integration cannot serve so no profile (nor UI/API) advertises them.
        root_local_registrations = build_local_tool_registrations(
            definitions=base_local_tools_definition,
            implementations=local_tool_implementations,
            metadata_by_name=local_tool_metadata_by_name,
        )
        root_local_registrations = filter_oauth_tool_registrations(
            root_local_registrations, google_integration_state
        )
        root_local_provider = LocalToolsProvider(
            registrations=root_local_registrations,
            embedding_generator=self.embedding_generator,
            calendar_config=_calendar_config_to_dict(self.config.calendar_config),
        )

        # Create root MCP provider with ALL configured servers
        # ast-grep-ignore: no-dict-any - Configuration model dump returns dynamic dict
        all_mcp_servers_config: dict[str, MCPServerConfig] = {
            server_id: cast("MCPServerConfig", server_config.model_dump())
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
        self.fastapi_app.state.tools_provider = self.root_tools_provider
        self.fastapi_app.state.tool_definitions = (
            await self.root_tools_provider.get_tool_definitions()
        )
        name_counts = Counter(
            d.get("function", {}).get("name", "")
            for d in self.fastapi_app.state.tool_definitions
        )
        duplicates = sorted(
            name for name, count in name_counts.items() if name and count > 1
        )
        if duplicates:
            message = (
                "Duplicate tool name(s) detected at startup. Gemini and other "
                "LLM providers reject tool lists containing duplicate function "
                f"declarations. Duplicates: {', '.join(duplicates)}. Rename, "
                "unregister, or filter one of the conflicting tools "
                "(e.g. disable the local tool or remove the MCP server that "
                "exposes it)."
            )
            logger.error(message)
            await self.root_tools_provider.close()
            raise RuntimeError(message)
        logger.info(
            f"Root ToolsProvider initialized with {len(self.fastapi_app.state.tool_definitions)} tools"
        )

        # Load file-based skills and create NoteRegistry
        # Builtin skills load first; user skills override builtins with the same name.
        all_skills = []
        skills_config = self.config.skills_config
        builtin_dir = (
            Path(skills_config.builtin_dir)
            if skills_config.builtin_dir
            else PACKAGE_ROOT / "skills" / "builtin"
        )
        if builtin_dir.is_dir():
            all_skills.extend(load_skills_from_directory(builtin_dir))
        user_dir = Path(skills_config.user_dir) if skills_config.user_dir else None
        if user_dir:
            all_skills.extend(load_skills_from_directory(user_dir))
        note_registry = NoteRegistry(all_skills) if all_skills else None
        for profile_conf in resolved_profiles:
            profile_id = profile_conf.id

            if profile_conf.remote_a2a:
                self._setup_remote_a2a_profile(profile_conf)
                continue

            logger.info(
                f"Initializing ProcessingService for profile ID: '{profile_id}'"
            )
            profile_proc_conf = profile_conf.processing_config
            profile_tools_conf = profile_conf.tools_config
            profile_tools_policy = profile_conf.tools_policy
            profile_operator_tools_policy = profile_conf.operator_tools_policy
            profile_chat_id_map = profile_conf.chat_id_to_name_map

            profile_llm_model = profile_proc_conf.llm_model or self.config.model

            if profile_id in self.llm_client_overrides:
                llm_client_for_profile = self.llm_client_overrides[profile_id]
                logger.info(
                    f"Profile '{profile_id}' using overridden LLM client: {type(llm_client_for_profile).__name__}"
                )
            else:
                if profile_proc_conf.enable_computer_use:
                    # Computer use rides on the Google client's request/response
                    # conversion, so a retrying client (whose fallback may be a
                    # different provider) and non-Google providers cannot carry it.
                    if profile_proc_conf.retry_config is not None:
                        raise ValueError(
                            f"Profile '{profile_id}' has enable_computer_use=True "
                            "with retry_config, which is unsupported (computer use "
                            "requires the single Google GenAI client)"
                        )
                    resolved_provider = profile_proc_conf.provider or (
                        "google" if profile_llm_model.startswith("gemini-") else None
                    )
                    if resolved_provider != "google":
                        raise ValueError(
                            f"Profile '{profile_id}' has enable_computer_use=True "
                            f"but provider is '{resolved_provider}' (must be 'google')"
                        )

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
                    # ast-grep-ignore: no-dict-any - Temporary dict passed to LLMClientFactory
                    client_config = {
                        "model": profile_llm_model,
                        "model_parameters": self.config.llm_parameters,
                    }
                    if profile_proc_conf.provider:
                        client_config["provider"] = profile_proc_conf.provider
                    if profile_proc_conf.enable_computer_use:
                        client_config["enable_computer_use"] = True
                    if profile_proc_conf.computer_use_excluded_functions:
                        client_config["computer_use_excluded_functions"] = (
                            profile_proc_conf.computer_use_excluded_functions
                        )

                    logger.info(
                        "Creating LLM client for profile '%s' with model='%s'%s",
                        profile_id,
                        profile_llm_model,
                        f", provider='{profile_proc_conf.provider}'"
                        if profile_proc_conf.provider
                        else "",
                    )

                llm_client_for_profile = LLMClientFactory.create_client(
                    config=client_config
                )
                logger.info(
                    f"Profile '{profile_id}' using client: {type(llm_client_for_profile).__name__}"
                )

            policy_engine = _build_profile_policy_engine(
                profile_id,
                profile_tools_policy,
                profile_operator_tools_policy,
                self.config.global_tools_policy,
            )
            # Get confirmation timeout from config, default to 3600 seconds (1 hour)
            confirmation_timeout = profile_tools_conf.confirmation_timeout_seconds

            # Build provider chain: Policy → root. The provider chain is
            # shared by all consumers (LLM loop, scripts, web UI listings),
            # so it must reflect the full set of tools the profile is allowed
            # to use. On-demand gating is an LLM-loop concern only and lives
            # in a sibling ``OnDemandToolsView`` below.
            policy_provider = PolicyEnforcingToolsProvider(
                wrapped_provider=self.root_tools_provider,
                policy_engine=policy_engine,
                confirmation_timeout=confirmation_timeout,
            )
            profile_tools_provider = TaintTrackingToolsProvider(
                policy_provider,
                taint_policy=merge_taint_policy_config(
                    base=self.config.taint_policy,
                    profile=profile_conf.taint_policy,
                ),
                confirmation_timeout=confirmation_timeout,
            )
            on_demand_tool_names = profile_tools_conf.get_on_demand_tool_names()
            on_demand_mcp_ids = set(profile_tools_conf.get_on_demand_mcp_server_ids())
            profile_on_demand_view: OnDemandToolsView | None = None
            if on_demand_tool_names or on_demand_mcp_ids:
                profile_on_demand_view = OnDemandToolsView(
                    wrapped_provider=profile_tools_provider,
                    on_demand_tool_names=on_demand_tool_names,
                    on_demand_mcp_server_ids=on_demand_mcp_ids,
                )
            await profile_tools_provider.get_tool_definitions()

            profile_grants = (
                set(profile_conf.visibility_grants)
                if profile_conf.visibility_grants
                else None
            )
            notes_provider = NotesContextProvider(
                get_db_context_func=self._get_db_context_for_provider,
                prompts=profile_proc_conf.prompts,
                attachment_registry=self.attachment_registry,
                visibility_grants=profile_grants,
                note_registry=note_registry,
            )
            calendar_provider = CalendarContextProvider(
                calendar_config=_calendar_config_to_dict(self.config.calendar_config),
                timezone=ZoneInfo(profile_proc_conf.timezone),
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
                    timezone=ZoneInfo(profile_proc_conf.timezone),
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
                timezone=ZoneInfo(profile_proc_conf.timezone),
                max_history_messages=profile_proc_conf.max_history_messages,
                history_max_age_hours=profile_proc_conf.history_max_age_hours,
                web_max_history_messages=profile_proc_conf.web_max_history_messages,
                web_history_max_age_hours=profile_proc_conf.web_history_max_age_hours,
                max_iterations=profile_proc_conf.max_iterations,
                context_pruning_min_turns=profile_proc_conf.context_pruning_min_turns,
                tools_config=profile_tools_conf,
                delegation_security_level=profile_proc_conf.delegation_security_level,
                allowed_delegation_sources=(
                    profile_proc_conf.allowed_delegation_sources
                ),
                id=profile_id,
                description=profile_conf.description
                or f"Processing profile: {profile_id}",
                visibility_grants=profile_grants,
                default_note_visibility_labels=(
                    profile_proc_conf.default_note_visibility_labels
                    if profile_proc_conf.default_note_visibility_labels is not None
                    else self.config.notes_config.default_visibility_labels or None
                ),
                required_note_visibility_labels=(
                    profile_proc_conf.required_note_visibility_labels
                ),
                allowed_note_visibility_labels=(
                    profile_proc_conf.allowed_note_visibility_labels
                ),
                allow_wake_llm=profile_proc_conf.allow_wake_llm,
                note_registry=note_registry,
                greeting_wav_path=profile_proc_conf.greeting_wav_path,
                poll_interval_seconds=profile_proc_conf.poll_interval_seconds,
                max_async_seconds=profile_proc_conf.max_async_seconds,
            )

            home_assistant_client_for_profile = self.home_assistant_clients.get(
                profile_id
            )
            camera_backend_for_profile = None

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
                            camera_backend_for_profile = camera_backend
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

            # Deep Research profiles get the pollable subclass so a delegated
            # run submits/polls instead of holding a worker for the whole
            # (potentially very long) research run. Direct chat use is
            # unaffected — handle_chat_interaction is inherited unchanged.
            processing_service_class = (
                DeepResearchProcessingService
                if is_deep_research_model(profile_llm_model)
                else ProcessingService
            )
            processing_service_instance = processing_service_class(
                llm_client=llm_client_for_profile,
                tools_provider=profile_tools_provider,
                service_config=service_config,
                context_providers=context_providers,
                server_url=self.config.server_url,
                app_config=self.config,
                attachment_registry=self.attachment_registry,
                event_sources=self.event_processor.sources
                if self.event_processor
                else None,
                processing_services_registry=self.processing_services_registry,
                home_assistant_client=home_assistant_client_for_profile,
                camera_backend=camera_backend_for_profile,
                on_demand_view=profile_on_demand_view,
                credential_resolvers=self.credential_resolvers,
                api_backend=self.api_backend,
            )

            self.processing_services_registry[profile_id] = processing_service_instance

        if not self.processing_services_registry:
            logger.critical("No processing service profiles initialized.")
            raise SystemExit("No processing service profiles initialized.")

        self.fastapi_app.state.processing_services = self.processing_services_registry
        self.fastapi_app.state.a2a_cancel_events = self.a2a_cancel_events
        self.fastapi_app.state.a2a_background_tasks = self.a2a_background_tasks

        candidate = self.processing_services_registry.get(default_service_profile_id)
        if candidate is not None and not isinstance(candidate, ProcessingService):
            raise SystemExit(
                f"Default service profile '{default_service_profile_id}' is a remote A2A profile. "
                f"The default profile must be a local ProcessingService."
            )
        self.default_processing_service = candidate
        if self.default_processing_service is None:
            logger.warning(
                f"Default service profile ID '{default_service_profile_id}' not found. Falling back to first available."
            )
            for pid, svc in self.processing_services_registry.items():
                if isinstance(svc, ProcessingService):
                    default_service_profile_id = pid
                    self.default_processing_service = svc
                    break
            if self.default_processing_service is None:
                raise SystemExit(
                    "No local ProcessingService profiles available for default."
                )

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
        self.email_indexer = EmailIndexer(
            pipeline=self.document_indexer.pipeline,
            attachment_registry=self.attachment_registry,
            app_config=self.config,
        )
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
                processing_service=self.default_processing_service,
                processing_services_registry=self.processing_services_registry,
                app_config=self.config,
                attachment_registry=self.attachment_registry,
                get_db_context_func=self._get_db_context_for_telegram,
                confirmation_service=self.confirmation_service,
                confirmation_result_waiters=self.confirmation_result_waiters,
                fastapi_app=self.fastapi_app,  # Pass FastAPI app for chat_interfaces access
                # use_batching argument removed
            )
            self.fastapi_app.state.telegram_service = self.telegram_service
            # Register telegram chat interface in the registry
            self.fastapi_app.state.chat_interfaces["telegram"] = (
                self.telegram_service.chat_interface
            )
            self.fastapi_app.state.confirmation_ui_managers["telegram"] = (
                self.telegram_service.confirmation_manager
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

            # Add webhook source if enabled
            if event_config.sources.webhook.enabled:
                self.webhook_source = WebhookEventSource()
                event_sources["webhook"] = self.webhook_source
                self.fastapi_app.state.webhook_source = self.webhook_source
                logger.info("Created WebhookEventSource for incoming webhooks")
            else:
                self.webhook_source = None

            if event_sources:
                sample_interval_hours = event_config.storage.sample_interval_hours

                assert self.database_engine is not None, (
                    "Database engine must be initialized before creating EventProcessor"
                )
                self.event_processor = EventProcessor(
                    sources=event_sources,
                    sample_interval_hours=sample_interval_hours,
                    config=cast(
                        "EventConditionEvaluatorConfig",
                        event_config.model_dump(),
                    ),
                    get_db_context_func=self._get_db_context_for_events,
                    timezone=ZoneInfo(
                        self.config.default_profile_settings.processing_config.timezone
                    ),
                    profile_wake_llm_flags={
                        profile.id: profile.processing_config.allow_wake_llm
                        for profile in self.config.service_profiles
                    },
                )
                logger.info(
                    f"Event processor initialized with {len(event_sources)} sources"
                )
            else:
                logger.info("Event system enabled but no event sources configured")

    def _setup_remote_a2a_profile(self, profile_conf: ServiceProfile) -> None:
        """Create a RemoteA2AService for a remote A2A profile."""
        from family_assistant.a2a.auth import A2AAuthConfig  # noqa: PLC0415
        from family_assistant.a2a.client import A2AClientWrapper  # noqa: PLC0415
        from family_assistant.a2a.remote_service import (  # noqa: PLC0415
            RemoteA2AService,
        )
        from family_assistant.processing.types import (  # noqa: PLC0415
            RemoteServiceConfig,
        )

        remote_config = profile_conf.remote_a2a
        assert remote_config is not None  # caller checked

        auth_config = A2AAuthConfig(
            type=remote_config.auth.type,
            token_env=remote_config.auth.token_env,
            header_name=remote_config.auth.header_name,
        )

        # Validate auth env vars at startup
        auth_errors = auth_config.validate_env_vars()
        if auth_errors:
            logger.warning(
                "Remote A2A profile '%s' has auth config issues: %s",
                profile_conf.id,
                "; ".join(auth_errors),
            )

        client = A2AClientWrapper(
            agent_url=remote_config.agent_url,
            auth_config=auth_config,
            timeout=remote_config.timeout_seconds,
        )

        # The async wall-clock cap is decoupled from the per-HTTP-call timeout: an
        # async delegation is polled across many short calls and may legitimately
        # run far longer than a single request, so default it to one hour rather
        # than killing the run at the timeout_seconds boundary. The cap only reaps
        # a genuinely orphaned remote run; the assistant can poll status within it.
        max_async_seconds = (
            remote_config.max_async_seconds
            if remote_config.max_async_seconds is not None
            else DEFAULT_REMOTE_MAX_ASYNC_SECONDS
        )
        service_config = RemoteServiceConfig(
            id=profile_conf.id,
            description=profile_conf.description
            or remote_config.skills_description
            or f"Remote A2A agent at {remote_config.agent_url}",
            delegation_security_level=profile_conf.processing_config.delegation_security_level,
            allowed_delegation_sources=profile_conf.processing_config.allowed_delegation_sources,
            confirmation_timeout_seconds=profile_conf.tools_config.confirmation_timeout_seconds,
            poll_interval_seconds=remote_config.poll_interval_seconds,
            max_async_seconds=max_async_seconds,
            timeout_seconds=remote_config.timeout_seconds,
        )

        service = RemoteA2AService(
            service_config=service_config,
            client=client,
        )

        self.processing_services_registry[profile_conf.id] = service
        logger.info(
            "Registered remote A2A profile '%s' -> %s",
            profile_conf.id,
            remote_config.agent_url,
        )

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

        worker_timezone = ZoneInfo(default_profile_conf.processing_config.timezone)

        # Build a pool of identically-configured in-process workers. Multiple
        # workers are required so a handler that parks on an in-process future
        # (e.g. a confirmation-gated delegated run) is unblocked by a sibling that
        # services the task resolving it; in-process only, since workers share
        # in-memory futures and registries within this event loop.
        worker_count = self.config.task_worker_count
        self.task_workers = [
            self._build_task_worker(
                default_timezone=worker_timezone,
                engine=self.create_worker_engine(),
            )
            for _ in range(worker_count)
        ]
        self.task_worker_tasks = [
            asyncio.create_task(worker.run()) for worker in self.task_workers
        ]
        logger.info(
            f"Started task worker pool with {worker_count} worker(s): "
            f"{[w.worker_id for w in self.task_workers]}"
        )

        # Start health monitoring for the task worker pool
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

        # Reconcile stale worker tasks asynchronously
        asyncio.create_task(self._reconcile_worker_tasks())

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

    async def _reconcile_worker_tasks(self) -> None:
        """Reconcile stale worker tasks against backend state on startup."""
        worker_config = self.config.ai_worker_config
        if not worker_config.enabled:
            return

        try:
            assert self.database_engine is not None
            async with get_db_context(self.database_engine) as db_ctx:
                backend = get_worker_backend(
                    worker_config.backend_type,
                    workspace_root=worker_config.workspace_mount_path,
                    docker_config=worker_config.docker,
                    kubernetes_config=worker_config.kubernetes,
                )
                reconciled = await reconcile_stale_tasks(db_ctx, backend)
                if reconciled:
                    logger.info(
                        f"Reconciled {reconciled} stale worker tasks on startup"
                    )
        except Exception:
            logger.warning(
                "Worker task reconciliation failed on startup", exc_info=True
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

                local_tz = self.default_processing_service.service_config.timezone

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
                        f"System event cleanup task scheduled for {next_3am_local} ({local_tz})"
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
                        f"System error log cleanup task scheduled for {next_3am_local} ({local_tz}) with {error_log_retention_days} day retention"
                    )
                except Exception as e:
                    # If task already exists, this is fine - just log it
                    logger.info(f"System error log cleanup task setup: {e}")

                # Upsert the worker task cleanup task
                try:
                    await db_ctx.tasks.enqueue(
                        task_id="system_worker_task_cleanup_daily",
                        task_type="worker_task_cleanup",
                        payload={"retention_hours": 48},
                        scheduled_at=next_3am_utc,
                        recurrence_rule="FREQ=DAILY;BYHOUR=3;BYMINUTE=0",
                        max_retries_override=5,
                    )
                    logger.info(
                        f"Worker task cleanup task scheduled for {next_3am_local} ({local_tz})"
                    )
                except Exception as e:
                    logger.info(f"Worker task cleanup task setup: {e}")

                # Upsert the stale delegation run reaper (runs hourly so a
                # stranded run that never retried is surfaced reasonably soon).
                try:
                    await db_ctx.tasks.enqueue(
                        task_id="system_delegation_run_cleanup_hourly",
                        task_type="delegation_run_cleanup",
                        payload={},
                        scheduled_at=datetime.now(UTC),
                        recurrence_rule="FREQ=HOURLY;BYMINUTE=0",
                        max_retries_override=5,
                    )
                    logger.info("Delegation run cleanup task scheduled (hourly)")
                except Exception as e:
                    logger.info(f"Delegation run cleanup task setup: {e}")

                # Upsert the completed automation cleanup task
                try:
                    await db_ctx.tasks.enqueue(
                        task_id="system_completed_automation_cleanup_daily",
                        task_type="completed_automation_cleanup",
                        payload={"retention_hours": 24},
                        scheduled_at=next_3am_utc,
                        recurrence_rule="FREQ=DAILY;BYHOUR=3;BYMINUTE=0",
                        max_retries_override=5,
                    )
                    logger.info(
                        f"Completed automation cleanup task scheduled for {next_3am_local} ({local_tz})"
                    )
                except Exception as e:
                    logger.warning(f"Completed automation cleanup task setup: {e}")

                try:
                    await enqueue_message_history_backfill_task(db_ctx)
                    logger.info("Message history backfill task scheduled")
                except Exception as e:
                    logger.warning(f"Message history backfill task setup: {e}")
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

    def create_worker_engine(self) -> AsyncEngine:
        """Provide a database engine for one pool worker.

        Each worker gets its OWN engine (its own connection) so that a worker
        parked inside a transaction — e.g. a confirmation-gated delegated run
        waiting on an in-process future — does not hold the single shared
        connection and block its siblings. On SQLite this matters most: the
        shared StaticPool hands out one connection, so without a dedicated engine
        a long-held worker transaction serializes the whole pool.

        Exception: an in-memory SQLite database (``:memory:``) cannot be shared
        across engines (each new engine gets a fresh, empty database), so in that
        case the single shared engine is reused. In-memory SQLite is test-only.
        """
        if self.database_engine is None:
            raise RuntimeError("database_engine must be set before workers")

        url = self.database_engine.url
        is_memory_sqlite = url.get_backend_name() == "sqlite" and (
            url.database is None or ":memory:" in url.database
        )
        if is_memory_sqlite:
            return self.database_engine

        engine = create_engine_with_sqlite_optimizations(
            url.render_as_string(hide_password=False)
        )
        set_engine_history_taint_epoch(
            engine,
            self.config.taint_policy.history_taint_epoch,
        )
        self.worker_engines.append(engine)
        return engine

    def _build_task_worker(
        self, default_timezone: ZoneInfo, engine: AsyncEngine
    ) -> TaskWorker:
        """Construct and fully configure a single TaskWorker for the pool.

        Every worker in the pool is built here so they share an identical handler
        set and the same shared dependencies (processing service, confirmation
        waiters/managers, etc.). They are interchangeable: any worker can pick up
        any queued task. Each worker is given its own ``engine`` (see
        :meth:`create_worker_engine`).
        """
        if self.default_processing_service is None:
            raise RuntimeError("default_processing_service must be set before workers")
        if self.embedding_generator is None:
            raise RuntimeError("embedding_generator must be set before workers")
        if self.fastapi_app is None:
            raise RuntimeError("fastapi_app must be set before workers")
        worker = TaskWorker(
            processing_service=self.default_processing_service,
            chat_interface=self.telegram_service.chat_interface
            if self.telegram_service
            else NullChatInterface(),
            calendar_config=_calendar_config_to_dict(self.config.calendar_config),
            timezone=default_timezone,
            embedding_generator=self.embedding_generator,
            # shutdown_event is likely handled internally by TaskWorker or passed differently
            indexing_source=getattr(
                self, "indexing_source", None
            ),  # Pass indexing source if available
            event_sources=self.event_processor.sources
            if self.event_processor
            else None,
            engine=engine,  # Dedicated per-worker engine
            chat_interfaces=self.fastapi_app.state.chat_interfaces,
            confirmation_result_waiters=self.confirmation_result_waiters,
            confirmation_ui_managers=self.fastapi_app.state.confirmation_ui_managers,
            notification_dispatcher=self.notification_dispatcher,
            stream_hub=self.fastapi_app.state.conversation_stream_hub,
        )
        worker.register_task_handler("log_message", task_wrapper_handle_log_message)
        if self.document_indexer:
            worker.register_task_handler(
                "process_uploaded_document", self.document_indexer.process_document
            )
        if self.email_indexer:
            worker.register_task_handler(
                "index_email", self.email_indexer.handle_index_email
            )
        worker.register_task_handler(
            EMAIL_INTAKE_ACTION_TASK_TYPE, handle_email_intake_action
        )
        if self.notes_indexer:
            worker.register_task_handler(
                "index_note", self.notes_indexer.handle_index_note
            )
        worker.register_task_handler("llm_callback", handle_llm_callback)
        worker.register_task_handler(
            "delegated_profile_run",
            worker.handle_delegated_profile_run,
        )
        worker.register_task_handler(
            "delegation_poll",
            worker.handle_delegation_poll,
        )
        worker.register_task_handler(
            "delegation_run_cleanup",
            worker.handle_delegation_run_cleanup,
        )
        worker.register_task_handler(
            "embed_and_store_batch", handle_embed_and_store_batch
        )
        worker.register_task_handler(
            "index_message_history_batch", handle_index_message_history_batch
        )
        worker.register_task_handler(
            "system_event_cleanup", handle_system_event_cleanup
        )
        worker.register_task_handler(
            "system_error_log_cleanup", handle_system_error_log_cleanup
        )
        worker.register_task_handler("script_execution", handle_script_execution)
        worker.register_task_handler(
            SCHEDULE_AUTOMATION_ADVANCE_TASK_TYPE,
            worker.handle_schedule_automation_advance,
        )
        worker.register_task_handler(
            CONFIRMATION_TOOL_EXECUTION_TASK_TYPE,
            handle_confirmation_tool_execution,
        )
        worker.register_task_handler("worker_task_cleanup", handle_worker_task_cleanup)
        worker.register_task_handler(
            "completed_automation_cleanup", handle_completed_automation_cleanup
        )
        worker.register_task_handler("reindex_document", self.handle_reindex_document)
        logger.info(f"Registered task handlers for worker {worker.worker_id}")
        return worker

    WORKER_INACTIVITY_TIMEOUT = 600

    async def _check_and_restart_workers(self) -> None:
        """Run one health-check pass over the pool, restarting dead/stuck workers.

        Each worker is checked individually so a single dead or stuck worker is
        restarted in place without disturbing its healthy siblings. Restarting
        reuses the same TaskWorker instance (same engine and handlers) on a fresh
        ``run()`` task; the new task replaces the old one at the same pool index.
        """
        for index, worker in enumerate(self.task_workers):
            worker_task = self.task_worker_tasks[index]

            if worker_task.done():
                if worker_task.cancelled():
                    logger.warning(
                        f"Task worker {worker.worker_id} was cancelled, restarting..."
                    )
                else:
                    try:
                        # This re-raises any exception that occurred in the task
                        worker_task.result()
                        logger.warning(
                            f"Task worker {worker.worker_id} exited normally, "
                            "restarting..."
                        )
                    except Exception as e:
                        logger.error(
                            f"Task worker {worker.worker_id} crashed with error: {e}",
                            exc_info=True,
                        )

                logger.info(f"Restarting task worker {worker.worker_id}...")
                self.task_worker_tasks[index] = asyncio.create_task(worker.run())
                continue

            # Check last activity time for this worker
            if worker.last_activity:
                time_since_activity = (
                    datetime.now(UTC) - worker.last_activity
                ).total_seconds()

                if time_since_activity > self.WORKER_INACTIVITY_TIMEOUT:
                    logger.error(
                        f"Task worker {worker.worker_id} appears stuck (no activity "
                        f"for {time_since_activity:.0f}s), cancelling and restarting..."
                    )
                    worker_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await worker_task

                    self.task_worker_tasks[index] = asyncio.create_task(worker.run())
                    logger.info(
                        f"Task worker {worker.worker_id} restarted after inactivity "
                        "timeout"
                    )

    async def _monitor_task_worker_health(self) -> None:
        """Monitors the health of the task worker pool and restarts dead workers."""
        HEALTH_CHECK_INTERVAL = 30  # Check every 30 seconds

        while not self.shutdown_event.is_set():
            try:
                await asyncio.sleep(HEALTH_CHECK_INTERVAL)

                if not self.task_workers or not self.task_worker_tasks:
                    continue

                await self._check_and_restart_workers()

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
        owned_tasks.extend(task for task in self.task_worker_tasks if not task.done())

        if owned_tasks:
            logger.info(f"Cancelling {len(owned_tasks)} owned background tasks...")
            for task in owned_tasks:
                task.cancel()
            await asyncio.gather(*owned_tasks, return_exceptions=True)
            logger.info("Owned background tasks cancelled.")

        # Cancel in-flight non-blocking A2A send tasks so a shutdown does not
        # leave their a2a_tasks rows stuck in 'working'.
        a2a_tasks = [t for t in self.a2a_background_tasks.values() if not t.done()]
        if a2a_tasks:
            logger.info(f"Cancelling {len(a2a_tasks)} in-flight A2A send tasks...")
            for task in a2a_tasks:
                task.cancel()
            await asyncio.gather(*a2a_tasks, return_exceptions=True)

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
                if service_instance.kind == "remote":
                    try:
                        await service_instance.close()
                    except Exception as e:
                        logger.error(
                            f"Error closing remote service '{profile_id}': {e}",
                            exc_info=True,
                        )
                elif (
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

        # Dispose dedicated per-worker engines (always created by us, so always
        # disposed). Skip any that alias the shared engine (in-memory SQLite).
        for worker_engine in self.worker_engines:
            if worker_engine is not self.database_engine:
                await worker_engine.dispose()
        if self.worker_engines:
            logger.info(f"Disposed {len(self.worker_engines)} worker engine(s).")
        self.worker_engines = []

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
        self,
        exec_context: ToolExecutionContext,
        # ast-grep-ignore: no-dict-any - Payload from generic task dispatch system (JSON deserialized)
        payload: dict[str, Any],
    ) -> None:
        await handle_reindex_document(
            exec_context, cast("ReindexDocumentPayload", payload)
        )
