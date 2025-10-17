from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from telegram import BotCommand, BotCommandScopeAllPrivateChats
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
)

from family_assistant.telegram.batching import (
    DefaultMessageBatcher,
    NoBatchMessageBatcher,
)
from family_assistant.telegram.handler import TelegramUpdateHandler
from family_assistant.telegram.interface import TelegramChatInterface
from family_assistant.telegram.ui import TelegramConfirmationUIManager

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI

    from family_assistant.processing import ProcessingService
    from family_assistant.services.attachment_registry import AttachmentRegistry
    from family_assistant.storage.context import DatabaseContext


logger = logging.getLogger(__name__)


class TelegramService:
    """Manages the Telegram bot application lifecycle and update handling."""

    def __init__(
        self,
        telegram_token: str,
        allowed_user_ids: list[int],
        developer_chat_id: int | None,
        processing_service: ProcessingService,  # Default processing service
        processing_services_registry: dict[
            str, ProcessingService
        ],  # Registry of all services
        app_config: dict[str, Any],  # Main application config
        attachment_registry: AttachmentRegistry,  # Changed from AttachmentService
        get_db_context_func: Callable[
            ..., contextlib.AbstractAsyncContextManager[DatabaseContext]
        ],
        fastapi_app: FastAPI | None = None,  # FastAPI app for accessing app.state
    ) -> None:
        """
        Initializes the Telegram Service.

        Args:
            telegram_token: The Telegram Bot API token.
            allowed_user_ids: List of chat IDs allowed to interact with the bot.
            developer_chat_id: Optional chat ID for sending error notifications.
            processing_service: The Default ProcessingService instance.
            processing_services_registry: Dictionary of all ProcessingService instances.
            app_config: The main application configuration dictionary.
            attachment_registry: The AttachmentRegistry instance for handling file attachments.
            get_db_context_func: Async context manager function to get a DatabaseContext.
        """
        logger.info("Initializing TelegramService...")
        self.application = ApplicationBuilder().token(telegram_token).build()
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
        self.fastapi_app = (
            fastapi_app  # Store FastAPI app for accessing chat_interfaces
        )

        # Store the Default ProcessingService instance in bot_data for access in handlers
        # This is for the default service used by the batcher.
        self.application.bot_data["processing_service"] = processing_service
        logger.info(
            "Stored Default ProcessingService instance in application.bot_data."
        )

        # Build slash command to profile ID map
        self.slash_command_to_profile_id_map: dict[str, str] = {}
        service_profiles = self.app_config.get("service_profiles", [])
        for profile_config in service_profiles:  # Renamed variable for clarity
            profile_id = profile_config.get("id")
            if not profile_id:
                continue
            for command in profile_config.get("slash_commands", []):
                if command in self.slash_command_to_profile_id_map:
                    logger.warning(
                        f"Slash command '{command}' is mapped to multiple profile IDs. "
                        f"Using '{self.slash_command_to_profile_id_map[command]}', "
                        f"ignoring mapping to '{profile_id}'."
                    )
                else:
                    self.slash_command_to_profile_id_map[command] = profile_id
        if self.slash_command_to_profile_id_map:
            logger.info(
                f"Initialized slash command to profile ID map: {self.slash_command_to_profile_id_map}"
            )

        # Instantiate Confirmation Manager
        self.confirmation_manager = TelegramConfirmationUIManager(
            application=self.application
        )

        # Instantiate the handler class, passing self (the service instance)
        # The handler will use self.processing_service (the default one) for batched messages.
        self.update_handler = TelegramUpdateHandler(
            telegram_service=self,
            allowed_user_ids=allowed_user_ids,
            developer_chat_id=developer_chat_id,
            processing_service=processing_service,  # Pass default service to handler
            get_db_context_func=get_db_context_func,
            message_batcher=None,
            confirmation_manager=self.confirmation_manager,
        )

        batching_config = self.app_config.get("message_batching_config", {})
        batching_strategy = batching_config.get(
            "strategy", "default"
        )  # Default to 'default' (which means DefaultMessageBatcher)
        batch_delay_seconds = batching_config.get("delay_seconds", 0.5)

        if batching_strategy == "none":
            self.message_batcher = NoBatchMessageBatcher(
                batch_processor=self.update_handler
            )
            logger.info("Using NoBatchMessageBatcher strategy.")
        else:  # Default to DefaultMessageBatcher
            self.message_batcher = DefaultMessageBatcher(
                batch_processor=self.update_handler,
                batch_delay_seconds=batch_delay_seconds,
            )
            logger.info(
                f"Using DefaultMessageBatcher strategy with delay: {batch_delay_seconds}s."
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
        tool_args: dict[str, Any],
        timeout: float,
    ) -> bool:
        """Public method to request confirmation, called by ConfirmingToolsProvider."""
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
        bot_commands_to_set = [
            BotCommand("start", "Start the bot and get a welcome message")
        ]

        # Add commands from service profiles
        # Ensure slash_command_to_profile_id_map keys include the leading slash
        # BotCommand expects command name without slash

        processed_command_names = set()  # To avoid duplicates if a command maps to multiple profiles (though map prevents this)

        for profile_config in self.app_config.get("service_profiles", []):
            profile_id = profile_config.get("id")
            profile_name = profile_config.get(
                "name", profile_id
            )  # Use profile name or ID for description

            for slash_command_str in profile_config.get("slash_commands", []):
                command_name = slash_command_str.lstrip("/")
                if command_name not in processed_command_names:
                    description = profile_config.get(
                        "description",  # Check for a general profile description
                        f"Activate {profile_name} mode",  # Fallback description
                    )
                    # Telegram has a 255 character limit for command descriptions
                    if len(description) > 255:
                        truncated_description = description[:252] + "..."
                        logger.warning(
                            f"Command '/{command_name}' description truncated from {len(description)} to 255 characters. "
                            f"Original: {description}"
                        )
                        description = truncated_description
                    # More specific description if available per command in future
                    # For now, use profile's name/description.
                    bot_commands_to_set.append(BotCommand(command_name, description))
                    processed_command_names.add(command_name)

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
            logger.error(f"Failed to set bot commands: {e}", exc_info=True)

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
                logger.error(f"Error stopping Telegram updater: {e}", exc_info=True)

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
                    logger.error(
                        f"RuntimeError shutting down Telegram application: {e}",
                        exc_info=True,
                    )
            except Exception as e:
                logger.error(
                    f"Error shutting down Telegram application: {e}", exc_info=True
                )
        else:
            logger.info("Telegram application instance not found for shutdown.")