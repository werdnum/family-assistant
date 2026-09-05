from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from telegram import BotCommand, BotCommandScopeAllPrivateChats, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
)

from family_assistant.services.user_identity import UserIdentityResolver
from family_assistant.telegram.batching import (
    DefaultMessageBatcher,
    NoBatchMessageBatcher,
)
from family_assistant.telegram.handler import TelegramUpdateHandler
from family_assistant.telegram.interface import TelegramChatInterface
from family_assistant.telegram.ui import TelegramConfirmationUIManager

if TYPE_CHECKING:
    from fastapi import FastAPI

    from family_assistant.config_models import AppConfig
    from family_assistant.processing import DelegatableService, ProcessingService
    from family_assistant.security.taint import TaintMetadata
    from family_assistant.services.attachment_registry import AttachmentRegistry
    from family_assistant.services.confirmation_service import ConfirmationService
    from family_assistant.services.confirmation_waiters import (
        ConfirmationResultWaiterRegistry,
    )
    from family_assistant.storage.database import Database
    from family_assistant.tools.types import (
        ConfirmationOutcome,
        ToolCallReviewAuthorization,
    )


logger = logging.getLogger(__name__)

TELEGRAM_API_REQUEST_TIMEOUT_SECONDS = 30

MAX_BOT_COMMAND_DESCRIPTION_LENGTH = 255


def build_profile_slash_command_map(app_config: AppConfig) -> dict[str, str]:
    """Map each profile slash command (leading '/' included) to its profile id."""
    command_map: dict[str, str] = {}
    for profile_config in app_config.service_profiles:
        profile_id = profile_config.id
        if not profile_id:
            continue
        for command in profile_config.slash_commands:
            if command in command_map:
                logger.warning(
                    f"Slash command '{command}' is mapped to multiple profile IDs. "
                    f"Using '{command_map[command]}', "
                    f"ignoring mapping to '{profile_id}'."
                )
            else:
                command_map[command] = profile_id
    return command_map


def build_tier_slash_command_map(app_config: AppConfig) -> dict[str, str]:
    """Map each model tier's slash command to its tier name.

    No collision check: `AppConfig` refuses a word two things claim, and a
    second check here could only ever disagree with it.
    """
    return {
        tier.slash_command: tier_name
        for tier_name, tier in app_config.model_tiers.items()
        if tier.slash_command is not None
    }


def build_bot_commands(app_config: AppConfig) -> list[BotCommand]:
    """The command menu Telegram shows: built-ins, profiles, then model tiers.

    Profile commands choose *which* assistant answers and tier commands choose
    how hard it thinks about one message, so both belong in the menu, and a
    tier's description says which of the two a reader is looking at.
    """
    commands = [
        BotCommand("start", "Start the bot and get a welcome message"),
        BotCommand("interrupt", "Stop the current request"),
    ]
    seen: set[str] = set()

    for profile_config in app_config.service_profiles:
        for slash_command in profile_config.slash_commands:
            name = slash_command.lstrip("/")
            if name in seen:
                continue
            description = (
                profile_config.description or f"Activate {profile_config.id} mode"
            )
            commands.append(
                BotCommand(name, _bot_command_description(name, description))
            )
            seen.add(name)

    for tier_name, tier in app_config.model_tiers.items():
        if tier.slash_command is None:
            continue
        name = tier.slash_command.lstrip("/")
        if name in seen:
            continue
        label = tier.label or tier_name
        description = f"{label} — {tier.description}" if tier.description else label
        commands.append(BotCommand(name, _bot_command_description(name, description)))
        seen.add(name)

    return commands


def _bot_command_description(command_name: str, description: str) -> str:
    """Telegram caps a command description at 255 characters."""
    if len(description) <= MAX_BOT_COMMAND_DESCRIPTION_LENGTH:
        return description
    logger.warning(
        f"Command '/{command_name}' description truncated from "
        f"{len(description)} to {MAX_BOT_COMMAND_DESCRIPTION_LENGTH} characters. "
        f"Original: {description}"
    )
    return description[: MAX_BOT_COMMAND_DESCRIPTION_LENGTH - 3] + "..."


class TelegramService:
    """Manages the Telegram bot application lifecycle and update handling."""

    def __init__(
        self,
        telegram_token: str,
        processing_service: ProcessingService,  # Default processing service
        processing_services_registry: dict[
            str, DelegatableService
        ],  # Registry of all services
        app_config: AppConfig,
        attachment_registry: AttachmentRegistry,  # Changed from AttachmentService
        database: Database,
        confirmation_service: ConfirmationService | None = None,
        confirmation_result_waiters: ConfirmationResultWaiterRegistry | None = None,
        fastapi_app: FastAPI | None = None,  # FastAPI app for accessing app.state
    ) -> None:
        """
        Initializes the Telegram Service.

        Args:
            telegram_token: The Telegram Bot API token.
            processing_service: The Default ProcessingService instance.
            processing_services_registry: Dictionary of all ProcessingService instances.
            app_config: The main application configuration (typed AppConfig model).
            attachment_registry: The AttachmentRegistry instance for handling file attachments.
            database: Handle for this deployment's database.
        """
        logger.info("Initializing TelegramService...")
        builder = (
            ApplicationBuilder()
            .token(telegram_token)
            .concurrent_updates(True)
            .connect_timeout(TELEGRAM_API_REQUEST_TIMEOUT_SECONDS)
            .read_timeout(TELEGRAM_API_REQUEST_TIMEOUT_SECONDS)
            .write_timeout(TELEGRAM_API_REQUEST_TIMEOUT_SECONDS)
            .pool_timeout(TELEGRAM_API_REQUEST_TIMEOUT_SECONDS)
        )
        if app_config.telegram_api_base_url:
            builder = builder.base_url(app_config.telegram_api_base_url)
            logger.info(
                f"Using custom Telegram API base URL: {app_config.telegram_api_base_url}"
            )
            # Derive base_file_url from base_url for file downloads
            # If base_url is "http://localhost:9000/bot", base_file_url should be
            # "http://localhost:9000/file/bot" to match Telegram's URL structure
            base_url = app_config.telegram_api_base_url.rstrip("/")
            if base_url.endswith("/bot"):
                base_file_url = base_url[:-4] + "/file/bot"
                builder = builder.base_file_url(base_file_url)
                logger.info(f"Using custom Telegram file URL: {base_file_url}")
        self.application = builder.build()
        self._was_started: bool = False
        self._last_error: Exception | None = None
        self.chat_interface = TelegramChatInterface(
            self.application, attachment_registry
        )

        # Use AttachmentRegistry (replaces AttachmentService)
        self.attachment_registry = attachment_registry
        logger.info("Using AttachmentRegistry for file operations")

        self.processing_service = processing_service  # Store default service
        self.processing_services_registry = (
            processing_services_registry  # Store registry
        )
        self.app_config = app_config  # Store app_config
        self.user_identity_resolver = UserIdentityResolver(app_config)
        self.confirmation_service = confirmation_service
        self.confirmation_result_waiters = confirmation_result_waiters
        self.fastapi_app = (
            fastapi_app  # Store FastAPI app for accessing chat_interfaces
        )

        # Store the Default ProcessingService instance in bot_data for access in handlers
        # This is for the default service used by the batcher.
        self.application.bot_data["processing_service"] = processing_service
        logger.info(
            "Stored Default ProcessingService instance in application.bot_data."
        )

        self.slash_command_to_profile_id_map = build_profile_slash_command_map(
            self.app_config
        )
        if self.slash_command_to_profile_id_map:
            logger.info(
                f"Initialized slash command to profile ID map: {self.slash_command_to_profile_id_map}"
            )
        self.slash_command_to_model_tier_map = build_tier_slash_command_map(
            self.app_config
        )
        if self.slash_command_to_model_tier_map:
            logger.info(
                f"Initialized slash command to model tier map: {self.slash_command_to_model_tier_map}"
            )

        # Instantiate Confirmation Manager
        self.confirmation_manager = TelegramConfirmationUIManager(
            application=self.application,
            confirmation_service=confirmation_service,
            confirmation_result_waiters=confirmation_result_waiters,
            user_identity_resolver=self.user_identity_resolver,
        )

        # Instantiate the handler class, passing self (the service instance)
        # The handler will use self.processing_service (the default one) for batched messages.
        self.update_handler = TelegramUpdateHandler(
            telegram_service=self,
            user_identity_resolver=self.user_identity_resolver,
            processing_service=processing_service,  # Pass default service to handler
            database=database,
            message_batcher=None,
            confirmation_manager=self.confirmation_manager,
        )

        batching_config = self.app_config.message_batching_config
        batching_strategy = batching_config.strategy
        batch_delay_seconds = batching_config.delay_seconds
        media_group_quiet_seconds = batching_config.media_group_quiet_seconds
        media_group_max_wait_seconds = batching_config.media_group_max_wait_seconds

        if batching_strategy == "none":
            self.message_batcher = NoBatchMessageBatcher(
                batch_processor=self.update_handler,
                media_group_quiet_seconds=media_group_quiet_seconds,
                media_group_max_wait_seconds=media_group_max_wait_seconds,
            )
            logger.info(
                f"Using NoBatchMessageBatcher strategy with media group quiet "
                f"delay {media_group_quiet_seconds}s, max wait "
                f"{media_group_max_wait_seconds}s."
            )
        else:  # Default to DefaultMessageBatcher
            self.message_batcher = DefaultMessageBatcher(
                batch_processor=self.update_handler,
                batch_delay_seconds=batch_delay_seconds,
                media_group_quiet_seconds=media_group_quiet_seconds,
                media_group_max_wait_seconds=media_group_max_wait_seconds,
            )
            logger.info(
                f"Using DefaultMessageBatcher strategy with delay "
                f"{batch_delay_seconds}s, media group quiet delay "
                f"{media_group_quiet_seconds}s, max wait "
                f"{media_group_max_wait_seconds}s."
            )
        self.update_handler.message_batcher = self.message_batcher

        self.update_handler.register_handlers()  # This now registers CommandHandlers too
        self.application.add_handler(
            CallbackQueryHandler(
                self.confirmation_manager.confirmation_callback_handler,
                pattern=r"^confirm:",
            )
        )
        logger.info("TelegramService initialized.")

    # Add the confirmation request method to the service, delegating to the handler
    async def request_confirmation_from_user(
        self,
        conversation_id: str,  # Changed from chat_id: int
        # Add interface_type and turn_id to match the Protocol for consistency,
        # even if they are just passed through.
        interface_type: str,
        turn_id: str | None,
        prompt_text: str,
        tool_name: str,
        # ast-grep-ignore: no-dict-any - tool args have varying keys per tool
        tool_args: dict[str, Any],
        timeout: float,
        target_user_id: str | None = None,
        tool_call_id: str | None = None,
        source_message_internal_id: int | None = None,
        wait_for_durable_execution: bool = True,
        taint_state_json: TaintMetadata | None = None,
        processing_profile_id: str | None = None,
        tool_call_review_authorization: ToolCallReviewAuthorization | None = None,
    ) -> ConfirmationOutcome:
        """Public method to request confirmation, called by policy enforcement."""
        # Delegate directly to the confirmation manager
        if self.confirmation_manager:
            return await self.confirmation_manager.request_confirmation(
                conversation_id=conversation_id,  # Pass string conversation_id
                interface_type=interface_type,  # Pass new arg
                turn_id=turn_id,  # Pass new arg
                prompt_text=prompt_text,
                tool_name=tool_name,
                tool_args=tool_args,
                timeout=timeout,
                target_user_id=target_user_id,
                tool_call_id=tool_call_id,
                source_message_internal_id=source_message_internal_id,
                wait_for_durable_execution=wait_for_durable_execution,
                taint_state_json=taint_state_json,
                processing_profile_id=processing_profile_id,
                tool_call_review_authorization=tool_call_review_authorization,
            )
        else:
            logger.error(
                "ConfirmationUIManager instance not available in TelegramService."
            )
            raise RuntimeError(
                "Confirmation mechanism not properly initialized in handler."
            )

    async def _set_bot_commands(self) -> None:
        """Sets the bot's commands visible in the Telegram interface."""
        bot_commands_to_set = build_bot_commands(self.app_config)

        try:
            await self.application.bot.set_my_commands(
                commands=bot_commands_to_set,
                scope=BotCommandScopeAllPrivateChats(),  # Commands primarily for private chats
            )
            logger.info(
                f"Set bot commands for private chats: {[cmd.command for cmd in bot_commands_to_set]}"
            )
            # Optionally set global commands or other scopes if needed
        except Exception as e:
            logger.exception(f"Failed to set bot commands: {e}")

    async def start_polling(self) -> None:
        """Initializes the application, sets commands, and starts polling for updates."""
        logger.info("Starting Telegram polling...")
        await self.application.initialize()
        await self.application.start()  # Starts the application components

        # Set bot commands after application is initialized and bot is available
        await self._set_bot_commands()

        if self.application.updater:
            # Use Update.ALL_TYPES to ensure all relevant updates are received
            await self.application.updater.start_polling(
                allowed_updates=Update.ALL_TYPES
            )
            self._was_started = True  # Mark as started
            self._last_error = None  # Clear last error on successful start
            logger.info("Telegram polling started successfully.")
        else:
            logger.error(
                "Application updater not available after start. Polling cannot begin."
            )
            # Consider raising an error or setting a state indicating failure

    @property
    def last_error(self) -> Exception | None:
        """Returns the last error encountered by the error handler."""
        return self._last_error

    async def stop_polling(self) -> None:
        """Stops the polling and shuts down the application gracefully."""
        self._was_started = False  # Mark as stopped (or stopping)
        if self.application and self.application.updater:
            logger.info("Stopping Telegram polling...")
            try:
                if self.application.updater.running:  # Check if polling before stopping
                    await self.application.updater.stop()
                    logger.info("Telegram polling stopped.")
                else:
                    logger.info("Telegram polling was not running.")
            except Exception as e:
                logger.exception(f"Error stopping Telegram updater: {e}")

        if self.application:
            logger.info("Shutting down Telegram application...")
            try:
                # First stop the application if it's running
                if self.application.running:
                    await self.application.stop()
                    logger.info("Telegram application stopped.")
                # Now we can safely shutdown
                await self.application.shutdown()
                logger.info("Telegram application shut down.")
            except RuntimeError as e:
                if "still running" in str(e):
                    logger.warning(
                        "Telegram application was still running during shutdown, forcing stop"
                    )
                    try:
                        await self.application.stop()
                        await self.application.shutdown()
                    except Exception as e2:
                        logger.error(f"Error forcing Telegram shutdown: {e2}")
                else:
                    logger.exception(
                        f"RuntimeError shutting down Telegram application: {e}"
                    )
            except Exception as e:
                logger.exception(f"Error shutting down Telegram application: {e}")
        else:
            logger.info("Telegram application instance not found for shutdown.")
