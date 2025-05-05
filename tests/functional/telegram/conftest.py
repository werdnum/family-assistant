import asyncio
import contextlib
import logging
from collections import namedtuple
from typing import Any, AsyncGenerator, Callable, Dict, List, NamedTuple, Optional
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine
from telegram.ext import Application, ContextTypes

# Mock the LLMInterface before it's imported by other modules if necessary
# or ensure mocks are injected properly.
from tests.mocks.mock_llm import RuleBasedMockLLMClient  # Or use AsyncMock

from family_assistant.llm import LLMInterface, LLMOutput # Import LLMOutput
from family_assistant.processing import ProcessingService
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.telegram_bot import (
    BatchProcessor,
    # Import necessary tools components
    AVAILABLE_FUNCTIONS as local_tool_functions,
    ConfirmationUIManager,
    NoBatchMessageBatcher,
    TelegramService,
    TelegramUpdateHandler,
)
from family_assistant.tools import (
    CompositeToolsProvider,
    LocalToolsProvider,
    ToolExecutionContext,
    TOOLS_DEFINITION as local_tools_definition, # Import tool definitions
)

# Define a named tuple to hold the fixture results for easier access
class TelegramHandlerTestFixture(NamedTuple):
    handler: TelegramUpdateHandler
    mock_bot: AsyncMock
    mock_telegram_service: AsyncMock # Add the missing field
    mock_llm: LLMInterface # Can be AsyncMock or RuleBasedMockLLMClient
    mock_confirmation_manager: AsyncMock
    processing_service: ProcessingService
    get_db_context_func: Callable[..., contextlib.AbstractAsyncContextManager[DatabaseContext]]
    tools_provider: CompositeToolsProvider # Or specific provider used


@pytest_asyncio.fixture(scope="function")
async def telegram_handler_fixture(
    test_db_engine: AsyncEngine, # Use the default SQLite engine from root conftest
) -> AsyncGenerator[TelegramHandlerTestFixture, None]:
    """
    Sets up the environment for testing TelegramUpdateHandler end-to-end.

    Provides:
    - Real TelegramUpdateHandler instance.
    - Real ProcessingService instance with Mock LLM.
    - Real ToolsProvider instance (Composite/Local).
    - Real NoBatchMessageBatcher.
    - Mocked telegram.Bot instance.
    - Mocked ConfirmationUIManager instance.
    - Function to get DatabaseContext for the test DB.
    """
    # 1. Mock External Dependencies
    # Use RuleBasedMockLLMClient - rules will be set per-test
    mock_llm = RuleBasedMockLLMClient(
        rules=[], # Start with empty rules
        default_response=LLMOutput(content="Default mock response (no rule matched)"),
    )

    mock_bot = AsyncMock(name="MockBot")
    mock_bot.send_message = AsyncMock()
    mock_bot.send_chat_action = AsyncMock()
    mock_bot.edit_message_text = AsyncMock()
    mock_bot.edit_message_reply_markup = AsyncMock()
    # Add other methods as needed by tests (e.g., answer_callback_query)

    mock_application = AsyncMock(spec=Application)
    mock_application.bot = mock_bot

    mock_confirmation_manager = AsyncMock(spec=ConfirmationUIManager)
    # Default confirmation to False unless overridden in a test
    mock_confirmation_manager.request_confirmation.return_value = False

    mock_telegram_service = AsyncMock(spec=TelegramService)
    mock_telegram_service.application = mock_application # Link mock app

    # 2. Instantiate Real Components with Mocks
    # Configure ToolsProvider (using Local for simplicity initially)
    # Ensure tools don't rely on external services not mocked
    # Instantiate with actual local tools
    local_tools_provider = LocalToolsProvider(
        definitions=local_tools_definition, # Use imported definitions
        implementations=local_tool_functions, # Use imported functions
        embedding_generator=None, # Add mock/real embedding generator if needed by tools
        calendar_config=None, # Add accepted calendar_config argument
    )
    tools_provider = CompositeToolsProvider([local_tools_provider])

    processing_service = ProcessingService(
        llm_client=mock_llm,
        tools_provider=tools_provider,
        prompts={},  # Add mock/real prompts if needed
        # Add missing required arguments with test defaults
        calendar_config={},
        timezone_str="UTC",
        max_history_messages=10,
        server_url="http://test-server:8000", # Placeholder URL for tests
        history_max_age_hours=24,
    )

    # Function to get DB context for the specific test engine
    async def get_test_db_context_func() -> contextlib.AbstractAsyncContextManager[DatabaseContext]:
        # get_db_context uses the globally patched engine by default
        return await get_db_context()

    # Instantiate Handler (Batcher needs processor, Handler needs batcher)
    # Handler will be assigned as the processor to the batcher after instantiation
    handler = TelegramUpdateHandler(
        telegram_service=mock_telegram_service, # Pass the mock service
        allowed_user_ids=[12345], # Example allowed user ID for tests
        developer_chat_id=None,
        processing_service=processing_service,
        get_db_context_func=get_test_db_context_func,
        message_batcher=None, # Will be set below
        confirmation_manager=mock_confirmation_manager,
    )
    batcher = NoBatchMessageBatcher(batch_processor=handler)
    handler.message_batcher = batcher # Assign the batcher

    # 3. Yield the fixture components
    yield TelegramHandlerTestFixture(
        handler=handler,
        mock_bot=mock_bot,
        mock_telegram_service=mock_telegram_service, # Yield the mock service
        mock_llm=mock_llm,
        mock_confirmation_manager=mock_confirmation_manager,
        processing_service=processing_service,
        get_db_context_func=get_test_db_context_func,
        tools_provider=tools_provider,
    )

    # 4. Teardown (implicit via pytest-asyncio and fixture scope)
    await tools_provider.close() # Ensure tools provider resources are closed
