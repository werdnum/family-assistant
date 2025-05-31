import contextlib
from collections.abc import AsyncGenerator, Callable
from typing import Any, NamedTuple
from unittest.mock import AsyncMock

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.assistant import Assistant
from family_assistant.llm import LLMInterface
from family_assistant.processing import ProcessingService
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.telegram_bot import (
    TelegramChatInterface,  # For type hinting mock_confirmation_manager
    TelegramUpdateHandler,
)
from family_assistant.tools import (
    ToolsProvider,  # Changed from CompositeToolsProvider for generality
)
from tests.mocks.mock_llm import LLMOutput as MockLLMOutput
from tests.mocks.mock_llm import RuleBasedMockLLMClient


# Define a named tuple to hold the fixture results for easier access
class TelegramHandlerTestFixture(NamedTuple):
    assistant: Assistant  # Add the assistant instance
    handler: TelegramUpdateHandler
    mock_bot: AsyncMock
    # mock_telegram_service is now assistant.telegram_service
    mock_llm: LLMInterface
    mock_confirmation_manager: AsyncMock  # This will be a mock on assistant.telegram_service.chat_interface.request_confirmation
    mock_application: AsyncMock  # Add mock_application field
    processing_service: (
        ProcessingService  # This is assistant.default_processing_service
    )
    tools_provider: (
        ToolsProvider  # This is assistant.default_processing_service.tools_provider
    )
    get_db_context_func: Callable[
        ..., contextlib.AbstractAsyncContextManager[DatabaseContext]
    ]


@pytest_asyncio.fixture(scope="function")
async def telegram_handler_fixture(
    test_db_engine: AsyncEngine,
) -> AsyncGenerator[TelegramHandlerTestFixture, None]:
    """
    Sets up the environment for testing TelegramUpdateHandler using the Assistant class.
    """
    # 1. Create Mock LLM
    mock_llm_client = RuleBasedMockLLMClient(
        rules=[],
        default_response=MockLLMOutput(
            content="Default mock response (no rule matched)"
        ),
    )

    # 2. Prepare Configuration for Assistant
    # Ensure this profile ID matches what Assistant expects or is configured as default
    test_profile_id = "default_assistant_test_profile"
    test_config: dict[str, Any] = {
        "telegram_token": "test_token_123:ABC",
        "allowed_user_ids": [123, 12345],  # Include user_id used in tests
        "developer_chat_id": None,
        "model": "mock-model-for-testing",  # Will be overridden
        "embedding_model": "mock-deterministic-embedder",
        "embedding_dimensions": 10,
        "database_url": str(test_db_engine.url),  # Use the test DB engine
        "server_url": "http://localhost:8123",  # Test server URL
        "document_storage_path": "/tmp/test_docs",
        "attachment_storage_path": "/tmp/test_attachments",
        "default_service_profile_id": test_profile_id,
        "service_profiles": [
            {
                "id": test_profile_id,
                "description": "Test default profile",
                "processing_config": {
                    "prompts": {"system_prompt": "Test System Prompt"},
                    "calendar_config": {},
                    "timezone": "UTC",
                    "max_history_messages": 5,
                    "history_max_age_hours": 1,
                    "llm_model": "mock-model-for-testing-profile",  # Will be overridden
                    "delegation_security_level": "none",  # Allow tools for tests
                },
                "tools_config": {
                    "enable_local_tools": [
                        "add_or_update_note"
                    ],  # Enable a common tool
                    "confirm_tools": [],  # No confirmation for tests unless specified
                    "mcp_initialization_timeout_seconds": 5,
                },
                "chat_id_to_name_map": {12345: "TestUser"},
                "slash_commands": [],
            }
        ],
        "mcp_config": {"mcpServers": {}},
        "indexing_pipeline_config": {"processors": []},
        "message_batching_config": {"strategy": "none"},  # Ensure NoBatchMessageBatcher
        "llm_parameters": {},
        # Add any other minimal required config keys by Assistant
        "openrouter_api_key": None,
        "gemini_api_key": None,
        "willyweather_api_key": None,
        "willyweather_location_id": None,
        "litellm_debug": False,
    }

    # 3. Instantiate Assistant with LLM Override
    assistant_app = Assistant(
        config=test_config,
        llm_client_overrides={test_profile_id: mock_llm_client},
    )

    # 4. Setup Dependencies
    # This will create TelegramService, ProcessingService, etc.
    # The TelegramService will use a real Application with a *real* Bot,
    # so we need to mock the bot *after* setup_dependencies.
    await assistant_app.setup_dependencies()

    # Ensure TelegramService and its application are created
    assert assistant_app.telegram_service is not None
    # The real application is assistant_app.telegram_service.application
    # We will create a separate mock_application for use with create_mock_context.

    # Mock the bot instance that will be used by both the real application (by patching)
    # and the mock_application (by assignment).
    mock_bot_instance = AsyncMock(name="MockBotInstance")
    mock_bot_instance.send_message = AsyncMock()
    mock_bot_instance.send_chat_action = AsyncMock()
    mock_bot_instance.edit_message_text = AsyncMock()
    mock_bot_instance.edit_message_reply_markup = AsyncMock()

    # Patch the .bot attribute of the *real* application instance used by the handler
    assert assistant_app.telegram_service.application is not None
    assistant_app.telegram_service.application.bot = mock_bot_instance

    # Create a fully mocked Application for create_mock_context
    # This mock_application will be passed to create_mock_context.
    # It needs to provide the same mock_bot_instance for context.bot.
    # It also needs bot_data and job_queue attributes for CallbackContext.
    from telegram.ext import Application, JobQueue  # Required for spec

    mock_application_for_context = AsyncMock(
        spec=Application, name="MockApplicationForContext"
    )
    mock_application_for_context.bot = mock_bot_instance
    mock_application_for_context.bot_data = {}  # Initialize as dict
    mock_application_for_context.bot_data_class = (
        dict  # For CallbackContext initialization
    )
    mock_application_for_context.job_queue = AsyncMock(
        spec=JobQueue, name="MockJobQueue"
    )

    # Mock the ConfirmationUIManager's request_confirmation method
    # The ConfirmationUIManager is now part of TelegramChatInterface
    assert assistant_app.telegram_service.chat_interface is not None
    # Ensure the chat_interface is indeed a TelegramChatInterface for this mock
    assert isinstance(
        assistant_app.telegram_service.chat_interface, TelegramChatInterface
    )

    # Create an AsyncMock for the method itself
    mock_request_confirmation_method = AsyncMock(
        return_value=False
    )  # Default to no confirmation

    # Patch the method on the confirmation_manager instance within TelegramService
    assert assistant_app.telegram_service is not None  # Ensure telegram_service exists
    assert assistant_app.telegram_service.confirmation_manager is not None
    original_request_confirmation = (
        assistant_app.telegram_service.confirmation_manager.request_confirmation
    )
    assistant_app.telegram_service.confirmation_manager.request_confirmation = (
        mock_request_confirmation_method
    )

    # Function to get DB context for the specific test engine
    def get_test_db_context_func() -> contextlib.AbstractAsyncContextManager[
        DatabaseContext
    ]:
        return get_db_context(engine=test_db_engine)  # Explicitly pass test engine

    # 5. Yield Fixture Components
    # Ensure default_processing_service and its tools_provider are set
    assert assistant_app.default_processing_service is not None
    assert assistant_app.default_processing_service.tools_provider is not None
    assert (
        assistant_app.telegram_service.update_handler is not None
    )  # Changed handler to update_handler

    fixture_tuple = TelegramHandlerTestFixture(
        assistant=assistant_app,
        handler=assistant_app.telegram_service.update_handler,  # Changed handler to update_handler
        mock_bot=mock_bot_instance,  # The bot from the real application, now mocked
        mock_llm=mock_llm_client,
        mock_confirmation_manager=mock_request_confirmation_method,  # The mocked method
        mock_application=mock_application_for_context,  # Pass the new mock application
        processing_service=assistant_app.default_processing_service,
        tools_provider=assistant_app.default_processing_service.tools_provider,
        get_db_context_func=get_test_db_context_func,
    )
    yield fixture_tuple

    # 6. Teardown
    # Restore original request_confirmation if it was patched
    if (
        assistant_app.telegram_service is not None  # Ensure telegram_service exists
        and assistant_app.telegram_service.confirmation_manager is not None
        and hasattr(
            assistant_app.telegram_service.confirmation_manager, "request_confirmation"
        )
        and assistant_app.telegram_service.confirmation_manager.request_confirmation
        is mock_request_confirmation_method
    ):
        assistant_app.telegram_service.confirmation_manager.request_confirmation = (
            original_request_confirmation
        )

    await assistant_app.stop_services()  # Gracefully stop assistant's services
