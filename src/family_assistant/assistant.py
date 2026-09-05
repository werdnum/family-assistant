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
from family_assistant.a2a.attachments import A2AAttachmentTransfer
from family_assistant.config_models import (
    DEFAULT_REMOTE_MAX_ASYNC_SECONDS,
    AppConfig,  # Used at runtime
    ProcessingConfig,  # Used at runtime
    RetryConfig,  # Used at runtime
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
from family_assistant.interfaces import ChatDeliveryError
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.llm.providers.google_genai_client import (
    is_antigravity_model,
    is_interactions_agent_model,
)
from family_assistant.observability.exporter import start_metrics_exporter
from family_assistant.paths import PACKAGE_ROOT
from family_assistant.processing import (
    DelegatableService,
    ProcessingService,
    ProcessingServiceConfig,
)
from family_assistant.processing.interactions_agent_service import (
    InteractionsAgentProcessingService,
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
from family_assistant.services.effective_tool_registry import (
    build_effective_local_tool_registrations,
)
from family_assistant.services.google_provider import GOOGLE_PROVIDER
from family_assistant.services.notification_dispatcher import NotificationDispatcher
from family_assistant.services.oauth_credentials import OAuthCredentialResolver
from family_assistant.services.oauth_integration_state import (
    OAuthIntegrationState,
    evaluate_oauth_integration_state,
)
from family_assistant.services.push_notification import PushNotificationService
from family_assistant.services.tool_call_review import ToolCallReviewer
from family_assistant.services.user_identity import UserIdentityResolver
from family_assistant.services.worker_backend import get_worker_backend
from family_assistant.skills import NoteRegistry, load_skills_from_directory
from family_assistant.storage import init_db
from family_assistant.storage.base import create_engine_with_sqlite_optimizations
from family_assistant.storage.database import (
    Database,
    set_engine_history_taint_epoch,
)
from family_assistant.task_worker import (
    SCHEDULE_AUTOMATION_ADVANCE_TASK_TYPE,
    ReindexDocumentPayload,
    TaskWorker,
    handle_attachment_cleanup,
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
    MAX_POLICY_RULE_PRIORITY,
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
    ToolsProvider,
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
    from collections.abc import Sequence
    from wsgiref.simple_server import WSGIServer

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.camera.protocol import CameraBackend
    from family_assistant.config_models import AIWorkerConfig, ServiceProfile
    from family_assistant.context_providers import ContextProvider
    from family_assistant.home_assistant_wrapper import HomeAssistantClientWrapper
    from family_assistant.llm import LLMInterface
    from family_assistant.security.taint import SinkClass
    from family_assistant.services.attachment_registry import AttachmentRegistry
    from family_assistant.storage.types import EventConditionEvaluatorConfig
    from family_assistant.tools import ToolRegistration
    from family_assistant.tools.types import CalendarConfig as CalendarConfigDict
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


def _calendar_config_to_dict(
    pydantic_config: PydanticCalendarConfig,
) -> CalendarConfigDict:
    """Convert Pydantic CalendarConfig to TypedDict format for tool functions."""
    return cast("CalendarConfigDict", pydantic_config.model_dump(exclude_none=True))


def _root_provider_for_profile(
    shared_root: ToolsProvider,
    profile_calendar_config: PydanticCalendarConfig | None,
    local_registrations: Sequence[ToolRegistration],
    mcp_provider: ToolsProvider,
    embedding_generator: EmbeddingGenerator | None,
) -> ToolsProvider:
    """The provider a profile's policy chain wraps.

    Calendar tools read the calendar from the `LocalToolsProvider` they were built
    with, so a profile naming its own calendar needs its own local provider.
    Sharing the root one would let the profile's prompt context and its tool calls
    disagree about which calendar it is looking at -- events listed from one, added
    to another. Profiles without their own calendar share the root provider; the
    MCP provider is shared either way, since nothing in it is calendar-scoped.
    """
    if profile_calendar_config is None:
        return shared_root
    return CompositeToolsProvider(
        providers=[
            LocalToolsProvider(
                registrations=list(local_registrations),
                embedding_generator=embedding_generator,
                calendar_config=_calendar_config_to_dict(profile_calendar_config),
            ),
            mcp_provider,
        ]
    )


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
    excluded_global_tools: Sequence[str] | None = None,
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

    synthetic_rules: list[PolicyRule] = []

    # Withheld global tools are denied FIRST, in the same layer the global rules
    # are injected into. Both position and priority are load-bearing.
    #
    # Priority alone is not enough: MAX_POLICY_RULE_PRIORITY is also the highest
    # a configured global rule may declare, so a global allow at 99 ties with
    # this deny. Ties resolve on declaration order within a layer, so the deny
    # has to be declared before the rules it overrides -- appended after them, a
    # priority-99 global allow wins and the profile silently keeps a tool it
    # asked to give up.
    #
    # The layer matters too: the equivalent rule in a profile's own
    # `tools_policy` cannot work at any priority, because that policy lands in
    # the lower-ranked `defaults` layer. Operator policy still outranks this,
    # which is intended -- the operator's word stays final.
    if excluded_global_tools:
        synthetic_rules.append(
            PolicyRule(
                match=ToolMatcher(names=list(excluded_global_tools)),
                decision=ToolPolicyDecision.DENY,
                priority=MAX_POLICY_RULE_PRIORITY,
                description=(
                    f"Profile '{profile_id}' withholds these globally granted tools."
                ),
            )
        )

    synthetic_rules.append(
        PolicyRule(
            match=ToolMatcher(
                names=["delegate_to_service"],
                argument_equals={"target_service_id": profile_id},
            ),
            decision=ToolPolicyDecision.ALLOW,
            priority=50,
            description=f"Allow self-delegation for profile '{profile_id}'",
        )
    )
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
    ) -> str:
        """Delivers nothing: there is no interface configured to deliver to."""
        _ = taint_metadata
        logger.debug(
            "NullChatInterface: send_message called for conversation %s: %s",
            conversation_id,
            text,
        )
        raise ChatDeliveryError(
            f"No chat interface is configured to deliver to {conversation_id}.",
            transient=False,
        )


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


def _antigravity_models_in_retry_config(retry_config: RetryConfig | None) -> list[str]:
    """Antigravity model ids named anywhere in a retry chain."""
    if retry_config is None:
        return []
    chain = [retry_config.primary.model]
    if retry_config.fallback is not None:
        chain.append(retry_config.fallback.model)
    return [model for model in chain if model and is_antigravity_model(model)]


def validate_antigravity_agent_config(
    profile_id: str,
    processing_config: ProcessingConfig,
    llm_model: str,
) -> None:
    """Reject Antigravity profile settings that would fail silently at runtime.

    The agent exists only on the Interactions API, reached through the Google
    client, so three configurations produce a plausible-looking answer instead
    of an error and are refused here:

    - ``antigravity_config`` on a profile that is not the agent, where the
      settings would simply be discarded;
    - the agent anywhere in a ``retry_config`` chain -- as a fallback it would
      never run, and as a primary the profile is built from the retry format,
      which carries neither ``antigravity_config`` nor (when ``llm_model`` is
      left unset) the model id the pollable-service selection reads, so a
      delegated run would silently take the inline path;
    - a non-Google ``provider`` alongside the agent's model id, which builds an
      OpenAI or Anthropic client and sends the agent id as an ordinary chat
      model.
    """
    is_antigravity_profile = is_antigravity_model(llm_model)
    if processing_config.antigravity_config and not is_antigravity_profile:
        raise ValueError(
            f"Profile '{profile_id}' sets antigravity_config but its model is "
            f"'{llm_model}', which is not an Antigravity managed agent"
        )

    retry_chain_models = _antigravity_models_in_retry_config(
        processing_config.retry_config
    )
    if retry_chain_models or (
        is_antigravity_profile and processing_config.retry_config is not None
    ):
        named = ", ".join(retry_chain_models) or llm_model
        raise ValueError(
            f"Profile '{profile_id}' uses the Antigravity managed agent "
            f"('{named}') with retry_config, which is unsupported (the agent "
            "requires the single Google GenAI client). Set it as llm_model with "
            "no retry_config instead."
        )

    if is_antigravity_profile and processing_config.provider != "google":
        raise ValueError(
            f"Profile '{profile_id}' uses the Antigravity managed agent but "
            f"provider is '{processing_config.provider}' (must be 'google' -- the "
            "agent only exists on Google's Interactions API)"
        )


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
        self.metrics_server: WSGIServer | None = None
        self.health_monitor_task: asyncio.Task | None = None  # Track health monitor
        self.event_processor_task: asyncio.Task | None = None  # Track event processor
        self._tool_call_reviewer: ToolCallReviewer | None = None
        self._is_shutdown_complete = False

        # Event system
        self.event_processor: EventProcessor | None = None
        # ast-grep-ignore: no-dict-any - maps profile IDs to heterogeneous HA client objects
        self.home_assistant_clients: dict[str, Any] = {}  # profile_id -> HA client

        # Logging handler
        self.error_logging_handler = None

    def _require_database_engine(self) -> AsyncEngine:
        """The application engine, which every worker and handle shares."""
        if not self.database_engine:
            raise RuntimeError("Database engine not initialized")
        return self.database_engine

    def _database(self) -> Database:
        """A handle on this deployment's database.

        Deferred rather than stored: components are wired before the engine
        exists, and the handle is stateless so building one per caller costs
        nothing.
        """
        return Database(self._require_database_engine())

    async def _ensure_playwright_browsers_installed(self) -> None:
        """Ensure Playwright browsers are installed, install if missing."""
        try:
            await self._check_or_install_playwright_browsers()
        except Exception as e:
            logger.warning(f"Could not check/install Playwright browsers: {e}")

    async def _check_or_install_playwright_browsers(self) -> None:
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
        await self._setup_application()
        self._setup_embedding_generator()
        await self._setup_database()
        self._setup_confirmation_interfaces()
        self._setup_attachment_registry()
        self._setup_error_logging()
        await self._setup_root_tools_provider()
        await self._setup_processing_services()
        self._select_default_processing_service()
        self._setup_indexers()
        self._setup_telegram_service()
        self._setup_event_system()

    async def _setup_application(self) -> None:
        """Create shared application state and validate model credentials."""
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

    def _setup_embedding_generator(self) -> None:
        """Build and publish the configured embedding generator."""
        assert self.fastapi_app is not None
        embedding_model_name = self.config.embedding_model
        embedding_dimensions = self.config.embedding_dimensions
        embedding_provider = self.config.embedding_provider
        if embedding_provider == "openai":
            self.embedding_generator = self._create_openai_embedding_generator()
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
            self.embedding_generator = self._create_local_embedding_generator(
                embedding_model_name
            )
        elif embedding_model_name.startswith(("gemini/", "gemini-")):
            self.embedding_generator = self._create_google_embedding_generator(
                embedding_model_name, embedding_dimensions
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

    def _create_openai_embedding_generator(self) -> OpenAIEmbeddingGenerator:
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
        explicit_dimensions = (
            self.config.embedding_dimensions
            if "embedding_dimensions" in self.config.model_fields_set
            else None
        )
        return OpenAIEmbeddingGenerator(
            model=self.config.embedding_model,
            api_key=api_key,
            base_url=self.config.embedding_base_url,
            dimensions=explicit_dimensions,
        )

    @staticmethod
    def _create_local_embedding_generator(
        embedding_model_name: str,
    ) -> EmbeddingGenerator:
        try:
            if "SentenceTransformerEmbeddingGenerator" not in dir(embeddings):
                raise ImportError("sentence-transformers library not installed.")
            return embeddings.SentenceTransformerEmbeddingGenerator(
                model_name_or_path=embedding_model_name
            )
        except Exception as exc:
            logger.critical(
                "Failed to initialize local embedding model '%s': %s",
                embedding_model_name,
                exc,
            )
            raise SystemExit(f"Local embedding model init failed: {exc}") from exc

    @staticmethod
    def _create_google_embedding_generator(
        embedding_model_name: str, embedding_dimensions: int
    ) -> GoogleEmbeddingGenerator:
        canonical_name = embedding_model_name
        if not canonical_name.startswith("gemini/"):
            canonical_name = f"gemini/{canonical_name}"
        if canonical_name == "gemini/":
            raise ValueError("Embedding model name cannot be just 'gemini/'.")
        return GoogleEmbeddingGenerator(
            model=canonical_name,
            dimensions=embedding_dimensions,
        )

    async def _setup_database(self) -> None:
        """Initialize and publish the application database engine."""
        assert self.fastapi_app is not None
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
            db_ctx = Database(self.database_engine)
            await db_ctx.init_vector_db()

        # Attach the deployment history taint epoch to the engine so every
        # Database (web, telegram, task worker, scripts) applies the same
        # read-time amnesty when materializing message-history taint metadata.
        set_engine_history_taint_epoch(
            self.database_engine,
            self.config.taint_policy.history_taint_epoch,
        )

        # Store engine in FastAPI app state for web dependencies
        self.fastapi_app.state.database_engine = self.database_engine

    def _setup_confirmation_interfaces(self) -> None:
        """Initialize confirmation and email delivery services."""
        assert self.fastapi_app is not None
        user_identity_resolver = self.fastapi_app.state.user_identity_resolver
        # Initialize notification channels (Web Push + iOS APNs) and the dispatcher that fans
        # out to all configured channels. Built before the confirmation service so it can be
        # injected as a dependency.
        self._setup_notifications()

        database_engine = self.database_engine
        assert database_engine is not None
        self.confirmation_service = ConfirmationService(
            db=Database(database_engine),
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

    def _setup_attachment_registry(self) -> None:
        """Initialize attachment storage and publish its registry."""
        assert self.fastapi_app is not None
        assert self.database_engine is not None
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

    def _setup_error_logging(self) -> None:
        """Enable database-backed error logging when configured."""
        assert self.database_engine is not None
        # Setup error logging to database if enabled
        error_logging_enabled = self.config.logging.database_errors.enabled
        # Also check environment variable to disable for testing
        if error_logging_enabled and not os.environ.get(
            "FAMILY_ASSISTANT_DISABLE_DB_ERROR_LOGGING"
        ):
            self.error_logging_handler = setup_error_logging(self.database_engine)
            logger.info("Database error logging handler initialized")

    async def _setup_root_tools_provider(self) -> None:
        """Build the shared local and MCP tool-provider root."""
        assert self.fastapi_app is not None
        assert self.shared_httpx_client is not None
        google_integration_state = self._setup_google_integration()

        logger.info("Creating root ToolsProvider with all available tools")
        root_local_registrations = build_effective_local_tool_registrations(
            self.config, google_integration_state
        )
        self._root_local_registrations = root_local_registrations
        root_local_provider = LocalToolsProvider(
            registrations=root_local_registrations,
            embedding_generator=self.embedding_generator,
            calendar_config=_calendar_config_to_dict(self.config.calendar_config),
        )

        all_mcp_servers_config: dict[str, MCPServerConfig] = {
            server_id: cast("MCPServerConfig", server_config.model_dump())
            for server_id, server_config in self.config.mcp_config.mcpServers.items()
        }
        root_mcp_provider = MCPToolsProvider(
            mcp_server_configs=all_mcp_servers_config,
            initialization_timeout_seconds=60,
        )
        self._root_mcp_provider = root_mcp_provider
        self.root_tools_provider = CompositeToolsProvider(
            providers=[root_local_provider, root_mcp_provider]
        )
        self.fastapi_app.state.tools_provider = self.root_tools_provider
        self.fastapi_app.state.tool_definitions = (
            await self.root_tools_provider.get_tool_definitions()
        )
        await self._reject_duplicate_tool_names()
        logger.info(
            "Root ToolsProvider initialized with %s tools",
            len(self.fastapi_app.state.tool_definitions),
        )

    def _setup_google_integration(self) -> OAuthIntegrationState:
        """Resolve and publish the Google integration state."""
        assert self.fastapi_app is not None
        assert self.shared_httpx_client is not None
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
        return google_integration_state

    async def _reject_duplicate_tool_names(self) -> None:
        """Close the root provider and fail startup on duplicate tool names."""
        assert self.fastapi_app is not None
        assert self.root_tools_provider is not None
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

    async def _setup_processing_services(self) -> None:
        """Build every configured local or remote processing service."""
        assert self.fastapi_app is not None
        assert self.root_tools_provider is not None
        assert self._root_mcp_provider is not None
        resolved_profiles = self.config.service_profiles
        note_registry = self._load_note_registry()
        delegation_sink_classes = {
            candidate.id: candidate.processing_config.taint_sink_class
            for candidate in resolved_profiles
            if candidate.processing_config.taint_sink_class is not None
        }
        tool_call_reviewer = self._create_tool_call_reviewer()
        self._tool_call_reviewer = tool_call_reviewer
        for profile_conf in resolved_profiles:
            await self._setup_processing_profile(
                profile_conf,
                note_registry,
                delegation_sink_classes,
                tool_call_reviewer,
            )

        if not self.processing_services_registry:
            logger.critical("No processing service profiles initialized.")
            raise SystemExit("No processing service profiles initialized.")

    def _load_note_registry(self) -> NoteRegistry | None:
        """Load builtin and user skills into the note registry."""
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
        return NoteRegistry(all_skills) if all_skills else None

    def _create_tool_call_reviewer(self) -> ToolCallReviewer | None:
        """Build the optional shared tool-call reviewer."""
        review_config = self.config.tool_call_review
        tool_call_reviewer: ToolCallReviewer | None = None
        if review_config is not None and review_config.enabled:
            review_llm_client = self.llm_client_overrides.get("__tool_call_reviewer__")
            if review_llm_client is None:
                review_llm_client = self.llm_client_overrides.get(
                    self.config.default_service_profile_id
                )
            if review_llm_client is None and self.llm_client_overrides:
                # Constructor overrides are used by tests and embedded callers to
                # prevent external provider access. Reuse one for the shared judge
                # unless a dedicated override was supplied.
                review_llm_client = next(iter(self.llm_client_overrides.values()))
            if review_llm_client is None:
                if review_config.retry_config is not None:
                    review_retry = review_config.retry_config.model_dump(
                        exclude_none=True
                    )
                    primary = review_retry.setdefault("primary", {})
                    primary.setdefault("provider", review_config.provider)
                    primary.setdefault("model", review_config.model)
                    primary.setdefault("model_parameters", self.config.llm_parameters)
                    fallback = review_retry.get("fallback")
                    if isinstance(fallback, dict):
                        fallback.setdefault(
                            "model_parameters", self.config.llm_parameters
                        )
                    # ast-grep-ignore: no-dict-any - Factory config is assembled dynamically.
                    review_client_config: dict[str, Any] = {
                        "retry_config": review_retry
                    }
                else:
                    review_client_config = {
                        "provider": review_config.provider,
                        "model": review_config.model,
                        "model_parameters": self.config.llm_parameters,
                    }

                def create_review_llm_client() -> LLMInterface:
                    return LLMClientFactory.create_client(config=review_client_config)

                tool_call_reviewer = ToolCallReviewer(
                    None,
                    review_config,
                    llm_client_factory=create_review_llm_client,
                )
            else:
                tool_call_reviewer = ToolCallReviewer(review_llm_client, review_config)
        return tool_call_reviewer

    async def _setup_processing_profile(
        self,
        profile_conf: ServiceProfile,
        note_registry: NoteRegistry | None,
        delegation_sink_classes: dict[str, SinkClass],
        tool_call_reviewer: ToolCallReviewer | None,
    ) -> None:
        """Build and register one configured processing service."""
        profile_id = profile_conf.id

        if profile_conf.remote_a2a:
            self._setup_remote_a2a_profile(profile_conf)
            return

        logger.info(f"Initializing ProcessingService for profile ID: '{profile_id}'")
        profile_proc_conf = profile_conf.processing_config
        profile_tools_conf = profile_conf.tools_config

        profile_llm_model = profile_proc_conf.llm_model or self.config.model

        llm_client_for_profile = self._create_profile_llm_client(
            profile_conf, profile_llm_model
        )

        (
            profile_tools_provider,
            profile_on_demand_view,
        ) = await self._build_profile_tools_provider(
            profile_conf, delegation_sink_classes, tool_call_reviewer
        )

        profile_grants = (
            set(profile_conf.visibility_grants)
            if profile_conf.visibility_grants
            else None
        )
        context_providers = self._build_profile_context_providers(
            profile_conf, note_registry, profile_grants
        )

        service_config = ProcessingServiceConfig(
            taint_sink_class=profile_proc_conf.taint_sink_class,
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
            allowed_delegation_sources=(profile_proc_conf.allowed_delegation_sources),
            id=profile_id,
            description=profile_conf.description or f"Processing profile: {profile_id}",
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
            include_aggregated_context=(profile_proc_conf.include_aggregated_context),
            note_registry=note_registry,
            greeting_wav_path=profile_proc_conf.greeting_wav_path,
            poll_interval_seconds=profile_proc_conf.poll_interval_seconds,
            max_async_seconds=profile_proc_conf.max_async_seconds,
        )

        home_assistant_client_for_profile = self.home_assistant_clients.get(profile_id)
        camera_backend_for_profile = self._create_camera_backend(profile_conf)

        # Interactions API agent profiles (Deep Research, Antigravity)
        # get the pollable subclass so a delegated run submits/polls
        # instead of holding a worker for the whole (potentially very
        # long) run. Direct chat use is unaffected —
        # handle_chat_interaction is inherited unchanged.
        processing_service_class = (
            InteractionsAgentProcessingService
            if is_interactions_agent_model(profile_llm_model)
            else ProcessingService
        )
        merged_taint_policy = merge_taint_policy_config(
            base=self.config.taint_policy, profile=profile_conf.taint_policy
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
            taint_policy=merged_taint_policy,
        )

        # Render once now so a template referencing a placeholder that no
        # longer exists -- {current_time} and {aggregated_other_context} moved
        # into the turn-context block -- is a startup failure rather than an
        # error the first time somebody talks to this profile.
        try:
            processing_service_instance.validate_system_prompt_renders()
        except (ValueError, TypeError) as exc:
            raise SystemExit(
                f"Profile '{profile_id}' has an invalid system_prompt: {exc}"
            ) from exc

        self.processing_services_registry[profile_id] = processing_service_instance

    @staticmethod
    def _create_camera_backend(
        profile_conf: ServiceProfile,
    ) -> CameraBackend | None:
        """Create the optional camera backend for one profile."""
        camera_config = profile_conf.processing_config.camera_config
        if camera_config is None or camera_config.backend != "reolink":
            return None
        try:
            from family_assistant.camera.reolink import (  # noqa: PLC0415
                create_reolink_backend,
            )

            camera_backend = create_reolink_backend(
                camera_config.cameras_config or None
            )
            if camera_backend is not None:
                logger.info(
                    "Camera backend initialized for profile '%s'", profile_conf.id
                )
                return camera_backend
            logger.warning(
                "Camera backend not created for profile '%s' "
                "(no config or reolink-aio unavailable)",
                profile_conf.id,
            )
        except ImportError:
            logger.warning("Reolink backend requested but reolink-aio not installed")
        except Exception:
            logger.exception(
                "Failed to create camera backend for profile '%s'", profile_conf.id
            )
        return None

    def _build_profile_context_providers(
        self,
        profile_conf: ServiceProfile,
        note_registry: NoteRegistry | None,
        profile_grants: set[str] | None,
    ) -> list[ContextProvider]:
        """Build and filter the aggregated-context sources for one profile."""
        assert self.attachment_registry is not None
        profile_config = profile_conf.processing_config
        providers: list[ContextProvider] = [
            NotesContextProvider(
                get_db_context_func=self._database,
                prompts=profile_config.prompts,
                attachment_registry=self.attachment_registry,
                visibility_grants=profile_grants,
                note_registry=note_registry,
            ),
            CalendarContextProvider(
                calendar_config=_calendar_config_to_dict(
                    profile_config.calendar_config or self.config.calendar_config
                ),
                timezone=ZoneInfo(profile_config.timezone),
                prompts=profile_config.prompts,
            ),
            KnownUsersContextProvider(
                chat_id_to_name_map=profile_conf.chat_id_to_name_map,
                prompts=profile_config.prompts,
            ),
        ]
        weather_provider = self._create_weather_context_provider(profile_conf)
        if weather_provider is not None:
            providers.append(weather_provider)
        home_assistant_provider = self._create_home_assistant_context_provider(
            profile_conf
        )
        if home_assistant_provider is not None:
            providers.append(home_assistant_provider)

        excluded = set(profile_config.excluded_context_providers)
        if not excluded:
            return providers
        return [provider for provider in providers if provider.name not in excluded]

    def _create_weather_context_provider(
        self, profile_conf: ServiceProfile
    ) -> WeatherContextProvider | None:
        api_key = self.config.willyweather_api_key
        location_id = self.config.willyweather_location_id
        if not api_key or not location_id or self.shared_httpx_client is None:
            return None
        profile_config = profile_conf.processing_config
        return WeatherContextProvider(
            location_id=location_id,
            api_key=api_key,
            prompts=profile_config.prompts,
            timezone=ZoneInfo(profile_config.timezone),
            httpx_client=self.shared_httpx_client,
        )

    def _create_home_assistant_context_provider(
        self, profile_conf: ServiceProfile
    ) -> HomeAssistantContextProvider | None:
        profile_config = profile_conf.processing_config
        api_url = profile_config.home_assistant_api_url
        token = profile_config.home_assistant_token
        template = profile_config.home_assistant_context_template
        if not api_url or not token:
            return None

        client = self._get_home_assistant_client(profile_conf, api_url, token)
        if not client or not template:
            logger.warning(
                "Home Assistant context provider for profile '%s' is partially "
                "configured but missing essential settings (URL, token, or "
                "template). Skipping.",
                profile_conf.id,
            )
            return None
        try:
            if (
                HomeAssistantContextProvider.__module__
                != "family_assistant.context_providers"
            ):
                return None
            provider = HomeAssistantContextProvider(
                api_url=api_url,
                token=token,
                context_template=template,
                prompts=profile_config.prompts,
                verify_ssl=profile_config.home_assistant_verify_ssl,
                client=client,
            )
        except ImportError:
            logger.warning(
                "homeassistant_api library is not installed, but Home Assistant "
                "context provider is configured. Skipping."
            )
            return None
        except Exception as exc:
            logger.exception(
                "Failed to initialize HomeAssistantContextProvider for profile "
                "'%s': %s",
                profile_conf.id,
                exc,
            )
            return None
        logger.info(
            "HomeAssistantContextProvider added for profile '%s'.", profile_conf.id
        )
        return provider

    def _get_home_assistant_client(
        self, profile_conf: ServiceProfile, api_url: str, token: str
    ) -> HomeAssistantClientWrapper | None:
        client_key = f"{api_url}:{token[:8]}..."
        cached_client = self.home_assistant_clients.get(client_key)
        if cached_client is not None:
            self.home_assistant_clients[profile_conf.id] = cached_client
            return cast("HomeAssistantClientWrapper", cached_client)

        client = create_home_assistant_client(
            api_url=api_url,
            token=token,
            verify_ssl=profile_conf.processing_config.home_assistant_verify_ssl,
        )
        if client is not None:
            self.home_assistant_clients[client_key] = client
            self.home_assistant_clients[profile_conf.id] = client
        return client

    async def _build_profile_tools_provider(
        self,
        profile_conf: ServiceProfile,
        delegation_sink_classes: dict[str, SinkClass],
        tool_call_reviewer: ToolCallReviewer | None,
    ) -> tuple[TaintTrackingToolsProvider, OnDemandToolsView | None]:
        """Apply policy, taint tracking, and on-demand visibility for a profile."""
        assert self.root_tools_provider is not None
        profile_proc_conf = profile_conf.processing_config
        profile_tools_conf = profile_conf.tools_config
        policy_engine = _build_profile_policy_engine(
            profile_conf.id,
            profile_conf.tools_policy,
            profile_conf.operator_tools_policy,
            self.config.global_tools_policy,
            profile_conf.excluded_global_tools,
        )
        confirmation_timeout = profile_tools_conf.confirmation_timeout_seconds
        profile_root_provider = _root_provider_for_profile(
            shared_root=self.root_tools_provider,
            profile_calendar_config=profile_proc_conf.calendar_config,
            local_registrations=self._root_local_registrations,
            mcp_provider=self._root_mcp_provider,
            embedding_generator=self.embedding_generator,
        )
        policy_provider = PolicyEnforcingToolsProvider(
            wrapped_provider=profile_root_provider,
            policy_engine=policy_engine,
            confirmation_timeout=confirmation_timeout,
        )
        merged_taint_policy = merge_taint_policy_config(
            base=self.config.taint_policy, profile=profile_conf.taint_policy
        )
        review_config = self.config.tool_call_review
        profile_tools_provider = TaintTrackingToolsProvider(
            policy_provider,
            taint_policy=merged_taint_policy,
            confirmation_timeout=confirmation_timeout,
            delegation_sink_classes=delegation_sink_classes,
            tool_call_reviewer=tool_call_reviewer,
            review_config=review_config,
            deployment_review_guidance=(
                review_config.guidance if review_config is not None else ""
            ),
            profile_review_guidance=profile_proc_conf.review_guidance,
            include_aggregated_context=profile_proc_conf.include_aggregated_context,
            # This provider is what every caller holds, so it is where tool
            # executions are counted -- whichever entry path reached them.
            profile=profile_conf.id,
        )
        on_demand_tool_names = profile_tools_conf.get_on_demand_tool_names()
        on_demand_mcp_ids = set(profile_tools_conf.get_on_demand_mcp_server_ids())
        profile_on_demand_view = None
        if on_demand_tool_names or on_demand_mcp_ids:
            profile_on_demand_view = OnDemandToolsView(
                wrapped_provider=profile_tools_provider,
                on_demand_tool_names=on_demand_tool_names,
                on_demand_mcp_server_ids=on_demand_mcp_ids,
            )
        await profile_tools_provider.get_tool_definitions()
        return profile_tools_provider, profile_on_demand_view

    def _create_profile_llm_client(
        self, profile_conf: ServiceProfile, profile_llm_model: str
    ) -> LLMInterface:
        """Use an override or create the configured client for one profile."""
        profile_id = profile_conf.id
        profile_proc_conf = profile_conf.processing_config
        if profile_id in self.llm_client_overrides:
            llm_client = self.llm_client_overrides[profile_id]
            logger.info(
                "Profile '%s' using overridden LLM client: %s",
                profile_id,
                type(llm_client).__name__,
            )
            return llm_client

        self._validate_computer_use_config(profile_conf, profile_llm_model)
        validate_antigravity_agent_config(
            profile_id, profile_proc_conf, profile_llm_model
        )
        client_config = self._build_profile_llm_client_config(
            profile_conf, profile_llm_model
        )
        llm_client = LLMClientFactory.create_client(config=client_config)
        logger.info(
            "Profile '%s' using client: %s", profile_id, type(llm_client).__name__
        )
        return llm_client

    @staticmethod
    def _validate_computer_use_config(
        profile_conf: ServiceProfile, profile_llm_model: str
    ) -> None:
        profile_proc_conf = profile_conf.processing_config
        if not profile_proc_conf.enable_computer_use:
            return
        if profile_proc_conf.retry_config is not None:
            raise ValueError(
                f"Profile '{profile_conf.id}' has enable_computer_use=True "
                "with retry_config, which is unsupported (computer use "
                "requires the single Google GenAI client)"
            )
        resolved_provider = profile_proc_conf.provider or (
            "google" if profile_llm_model.startswith("gemini-") else None
        )
        if resolved_provider != "google":
            raise ValueError(
                f"Profile '{profile_conf.id}' has enable_computer_use=True "
                f"but provider is '{resolved_provider}' (must be 'google')"
            )

    def _build_profile_llm_client_config(
        self,
        profile_conf: ServiceProfile,
        profile_llm_model: str,
        # ast-grep-ignore: no-dict-any - Factory config has varying provider keys.
    ) -> dict[str, Any]:
        profile_proc_conf = profile_conf.processing_config
        if profile_proc_conf.retry_config is not None:
            return self._build_retry_client_config(profile_conf)

        # ast-grep-ignore: no-dict-any - Factory config has varying provider keys.
        client_config: dict[str, Any] = {
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
        if profile_proc_conf.antigravity_config:
            client_config.update({
                "antigravity_model": profile_proc_conf.antigravity_config.model,
                "antigravity_max_total_tokens": (
                    profile_proc_conf.antigravity_config.max_total_tokens
                ),
                "antigravity_environment": (
                    profile_proc_conf.antigravity_config.environment
                ),
            })
        logger.info(
            "Creating LLM client for profile '%s' with model='%s'%s",
            profile_conf.id,
            profile_llm_model,
            f", provider='{profile_proc_conf.provider}'"
            if profile_proc_conf.provider
            else "",
        )
        return client_config

    def _build_retry_client_config(
        self,
        profile_conf: ServiceProfile,
        # ast-grep-ignore: no-dict-any - Factory config has varying retry keys.
    ) -> dict[str, Any]:
        retry_config = profile_conf.processing_config.retry_config
        assert retry_config is not None
        retry_config_dict = retry_config.model_dump(exclude_none=True)
        llm_params = self.config.llm_parameters
        primary = retry_config_dict.get("primary")
        if primary is not None and "model_parameters" not in primary:
            primary["model_parameters"] = llm_params
        fallback = retry_config_dict.get("fallback")
        if fallback and "model_parameters" not in fallback:
            fallback["model_parameters"] = llm_params
        logger.info(
            "Creating RetryingLLMClient for profile '%s' with primary='%s', "
            "fallback='%s'",
            profile_conf.id,
            primary.get("model") if primary else None,
            fallback.get("model") if fallback else None,
        )
        return {"retry_config": retry_config_dict}

    def _select_default_processing_service(self) -> None:
        """Select and publish the default local processing service."""
        assert self.fastapi_app is not None
        default_service_profile_id = self.config.default_service_profile_id
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

    def _setup_indexers(self) -> None:
        """Initialize document, email, and notes indexing."""
        assert self.fastapi_app is not None
        assert self.default_processing_service is not None
        assert self.embedding_generator is not None
        assert self.attachment_registry is not None
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

    def _setup_telegram_service(self) -> None:
        """Initialize Telegram delivery when enabled."""
        assert self.fastapi_app is not None
        assert self.attachment_registry is not None
        assert self.confirmation_service is not None
        assert self.confirmation_result_waiters is not None
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
                database=self._database(),
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

    def _setup_event_system(self) -> None:
        """Initialize configured event sources and their processor."""
        assert self.fastapi_app is not None
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
                    get_db_context_func=self._database,
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

        if self.attachment_registry is None:
            raise RuntimeError(
                "AttachmentRegistry must be initialized before remote A2A profiles."
            )
        attachments = A2AAttachmentTransfer(
            attachment_registry=self.attachment_registry,
            db_context=self._database(),
        )

        client = A2AClientWrapper(
            agent_url=remote_config.agent_url,
            auth_config=auth_config,
            timeout=remote_config.timeout_seconds,
            attachments=attachments,
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
            attachments=attachments,
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

        if self.config.metrics_enabled:
            self.metrics_server = start_metrics_exporter(
                self.config.metrics_port, addr=self.config.metrics_bind_host
            )

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
                engine=self._require_database_engine(),
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
            await self._reconcile_stale_worker_tasks(worker_config)
        except Exception:
            logger.warning(
                "Worker task reconciliation failed on startup", exc_info=True
            )

    async def _reconcile_stale_worker_tasks(
        self, worker_config: AIWorkerConfig
    ) -> None:
        """Reconcile stale tasks with the configured worker backend."""
        assert self.database_engine is not None
        db_ctx = Database(self.database_engine)
        backend = get_worker_backend(
            worker_config.backend_type,
            workspace_root=worker_config.workspace_mount_path,
            docker_config=worker_config.docker,
            kubernetes_config=worker_config.kubernetes,
        )
        reconciled = await reconcile_stale_tasks(db_ctx, backend)
        if reconciled:
            logger.info(f"Reconciled {reconciled} stale worker tasks on startup")

    async def _setup_system_tasks(self) -> None:
        """Upsert system tasks on startup."""

        async def setup_tasks() -> None:
            assert self.database_engine is not None, (
                "Database engine must be initialized before setting up system tasks"
            )
            db_ctx = Database(self.database_engine)
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

            # Upsert the unreferenced attachment reaper. Uploads commit their
            # row before the message that references them exists, so a send
            # that never persists a message leaves the row and file behind.
            try:
                await db_ctx.tasks.enqueue(
                    task_id="system_attachment_cleanup_daily",
                    task_type="attachment_cleanup",
                    payload={"grace_hours": 24},
                    scheduled_at=next_3am_utc,
                    recurrence_rule="FREQ=DAILY;BYHOUR=3;BYMINUTE=0",
                    max_retries_override=5,
                )
                logger.info(
                    f"Attachment cleanup task scheduled for {next_3am_local} ({local_tz})"
                )
            except Exception:
                # The enqueue upserts, so a failure here is a real one, and it
                # leaves the reaper with no caller until the next restart.
                logger.exception("Attachment cleanup task setup failed")

            try:
                await enqueue_message_history_backfill_task(db_ctx)
                logger.info("Message history backfill task scheduled")
            except Exception as e:
                logger.warning(f"Message history backfill task setup: {e}")

        try:
            await setup_tasks()
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

    def _build_task_worker(
        self, default_timezone: ZoneInfo, engine: AsyncEngine
    ) -> TaskWorker:
        """Construct and fully configure a single TaskWorker for the pool.

        Every worker in the pool is built here so they share an identical handler
        set and the same shared dependencies (processing service, confirmation
        waiters/managers, etc.). They are interchangeable: any worker can pick up
        any queued task, and they share the application engine -- one database,
        one engine, one connection pool.
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
        worker.register_task_handler("attachment_cleanup", handle_attachment_cleanup)
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
                        logger.exception(
                            f"Task worker {worker.worker_id} crashed with error: {e}"
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
                logger.exception(f"Error in task worker health monitor: {e}")
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

        # Closed before anything else waits: the exporter owns a listening
        # socket on its own thread, and a test that starts two Assistants in a
        # row would otherwise fail to bind the second.
        if self.metrics_server is not None:
            self.metrics_server.shutdown()
            self.metrics_server.server_close()
            self.metrics_server = None

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
                        logger.exception(
                            f"Error closing remote service '{profile_id}': {e}"
                        )
                elif (
                    hasattr(service_instance, "tools_provider")
                    and service_instance.tools_provider
                ):
                    try:
                        await service_instance.tools_provider.close()
                    except Exception as e:
                        logger.exception(
                            f"Error closing tools_provider for profile '{profile_id}': {e}"
                        )
        elif (
            self.default_processing_service
            and self.default_processing_service.tools_provider
        ):
            logger.warning(
                "Processing services registry not found, closing default tools_provider."
            )
            await self.default_processing_service.tools_provider.close()

        if self._tool_call_reviewer is not None:
            try:
                await self._tool_call_reviewer.close()
            except Exception:
                logger.exception("Error closing shared tool-call reviewer")

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
        self,
        exec_context: ToolExecutionContext,
        # ast-grep-ignore: no-dict-any - Payload from generic task dispatch system (JSON deserialized)
        payload: dict[str, Any],
    ) -> None:
        await handle_reindex_document(
            exec_context, cast("ReindexDocumentPayload", payload)
        )
