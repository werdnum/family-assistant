import contextlib
from collections.abc import AsyncGenerator, Callable
from typing import NamedTuple, cast
from unittest.mock import AsyncMock

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine
from telegram.ext import Application

from family_assistant.llm import LLMInterface  # Import LLMOutput
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.telegram_bot import (
    ConfirmationUIManager,
    NoBatchMessageBatcher,
    TelegramService,
    TelegramUpdateHandler,  # Keep this as it's from telegram_bot
)
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS as local_tool_functions,
)
from family_assistant.tools import (
    TOOLS_DEFINITION as local_tools_definition,
)

# Correct imports for tools - they are in family_assistant.tools, not telegram_bot
from family_assistant.tools import (
    CompositeToolsProvider,
    LocalToolsProvider,
    calendar_integration,
)
from tests.mocks.mock_llm import LLMOutput as MockLLMOutput  # Import mock's LLMOutput

# Mock the LLMInterface before it's imported by other modules if necessary
# or ensure mocks are injected properly.
from tests.mocks.mock_llm import RuleBasedMockLLMClient


# Define a named tuple to hold the fixture results for easier access
class TelegramHandlerTestFixture(NamedTuple):
    handler: TelegramUpdateHandler
    mock_bot: AsyncMock
    mock_telegram_service: AsyncMock  # Add the missing field
    mock_llm: LLMInterface  # Can be AsyncMock or RuleBasedMockLLMClient
    mock_confirmation_manager: (
        AsyncMock  # Keep the mock for request_confirmation control
    )
    processing_service: ProcessingService
    tools_provider: CompositeToolsProvider  # The main tools provider
    get_db_context_func: Callable[
        ..., contextlib.AbstractAsyncContextManager[DatabaseContext]
    ]


@pytest_asyncio.fixture(scope="function")
async def telegram_handler_fixture(
    test_db_engine: AsyncEngine,  # Use the default SQLite engine from root conftest
) -> AsyncGenerator[TelegramHandlerTestFixture, None]:
    """
    Sets up the environment for testing TelegramUpdateHandler end-to-end.

    Provides:
    - Real TelegramUpdateHandler instance.
    - Real ProcessingService instance with Mock LLM.
    - Real ToolsProvider instance (Composite/Local).
    - Real NoBatchMessageBatcher.    - Real ConfirmingToolsProvider wrapping the CompositeProvider.
    - Mocked telegram.Bot instance.
    - Function to get DatabaseContext for the test DB.
    """
    # 1. Mock External Dependencies
    # Use RuleBasedMockLLMClient - rules will be set per-test
    mock_llm = RuleBasedMockLLMClient(
        rules=[],  # Start with empty rules
        default_response=MockLLMOutput(
            content="Default mock response (no rule matched)"
        ),  # Use MockLLMOutput here
    )

    mock_bot = AsyncMock(name="MockBot")
    mock_bot.send_message = AsyncMock()
    mock_bot.send_chat_action = AsyncMock()
    mock_bot.edit_message_text = AsyncMock()
    mock_bot.edit_message_reply_markup = AsyncMock()
    # Add other methods as needed by tests (e.g., answer_callback_query)

    mock_application = AsyncMock(spec=Application)
    mock_application.bot = mock_bot

    # Mock the UIManager that the *TelegramUpdateHandler* uses for requesting confirmation
    mock_confirmation_manager = AsyncMock(spec=ConfirmationUIManager)
    # Default confirmation to False unless overridden in a test
    # Tests will set return_value or side_effect as needed
    mock_confirmation_manager.request_confirmation.return_value = False

    mock_telegram_service = AsyncMock(spec=TelegramService)
    mock_telegram_service.application = mock_application  # Link mock app

    # 2. Instantiate Real Components with Mocks
    # Configure ToolsProvider (using Local for simplicity initially)
    # Ensure tools don't rely on external services not mocked
    # Add calendar tool implementation to local functions if not already there
    test_local_tool_functions = local_tool_functions.copy()
    if "delete_calendar_event" not in test_local_tool_functions:
        test_local_tool_functions["delete_calendar_event"] = (
            calendar_integration.delete_calendar_event_tool
        )
        # Add others if needed: modify_calendar_event, add_calendar_event, search_calendar_events

    # --- Modify Tool Definitions for Test ---
    # Create a deep copy to avoid modifying the original definition list
    import copy

    test_local_tools_definition = copy.deepcopy(local_tools_definition)

    # Instantiate with actual local tools
    local_tools_provider = LocalToolsProvider(
        definitions=test_local_tools_definition,  # Use the UNMODIFIED definitions for this fixture
        implementations=test_local_tool_functions,  # Use potentially extended functions
        embedding_generator=None,  # Add mock/real embedding generator if needed by tools
        calendar_config=None,  # Add accepted calendar_config argument
    )
    composite_provider = CompositeToolsProvider([local_tools_provider])

    # Create a ProcessingServiceConfig instance for the test
    test_service_config_obj = ProcessingServiceConfig(
        prompts={},  # Add mock/real prompts if needed
        calendar_config={},
        timezone_str="UTC",
        max_history_messages=10,
        history_max_age_hours=24,
    )

    # Define a separate app_config for the test
    test_app_config = {}

    processing_service = ProcessingService(
        llm_client=cast("LLMInterface", mock_llm),
        # Initialize with the non-confirming provider by default
        tools_provider=composite_provider,
        service_config=test_service_config_obj,  # Pass the ProcessingServiceConfig instance
        app_config=test_app_config,  # Pass the separate app_config
        context_providers=[],
        server_url="http://test-server:8000",  # Placeholder URL for tests
    )

    # Function to get DB context for the specific test engine
    def get_test_db_context_func() -> contextlib.AbstractAsyncContextManager[
        DatabaseContext
    ]:
        # get_db_context uses the globally patched engine by default
        return get_db_context()

    # Instantiate Handler (Batcher needs processor, Handler needs batcher)
    # Handler will be assigned as the processor to the batcher after instantiation
    handler = TelegramUpdateHandler(
        telegram_service=mock_telegram_service,  # Pass the mock service
        allowed_user_ids=[12345],  # Example allowed user ID for tests
        developer_chat_id=None,
        processing_service=processing_service,
        get_db_context_func=get_test_db_context_func,
        message_batcher=None,  # Will be set below
        confirmation_manager=mock_confirmation_manager,
    )
    batcher = NoBatchMessageBatcher(batch_processor=handler)
    handler.message_batcher = batcher  # Assign the batcher

    # 3. Yield the fixture components
    yield TelegramHandlerTestFixture(
        handler=handler,
        mock_bot=mock_bot,
        mock_telegram_service=mock_telegram_service,  # Yield the mock service
        mock_llm=cast("LLMInterface", mock_llm),
        mock_confirmation_manager=mock_confirmation_manager,
        processing_service=processing_service,
        tools_provider=composite_provider,  # Yield the composite provider
        get_db_context_func=get_test_db_context_func,  # Correct assignment
    )

    # 4. Teardown (implicit via pytest-asyncio and fixture scope)
    await composite_provider.close()  # Close the main provider
