# --- Testing Philosophy ---
# These tests focus on the end-to-end behavior of Telegram slash commands,
# specifically their routing to different processing profiles.
# Assertions primarily check:
# 1. Correct ProcessingService instance is invoked (verified by LLM call context like system prompt).
# 2. Messages SENT by the bot (verified via telegram_client).
# 3. LLM interaction (mocked LLM calls and responses).

import logging
import typing
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import telegramify_markdown  # type: ignore[import-untyped]
from assertpy import assert_that, soft_assertions
from telegram import Chat, Message, Update, User
from telegram.ext import Application, ContextTypes

from family_assistant.config_models import AppConfig
from family_assistant.llm import (
    LLMOutput as LLMResponseOutput,  # Renamed to avoid clash
)
from family_assistant.processing import ProcessingService, ProcessingServiceConfig

# Import mock LLM helpers
from tests.mocks.mock_llm import (
    MatcherArgs,
    RuleBasedMockLLMClient,
    get_last_message_text,
    get_system_prompt,
)

# Import the fixture and its type hint
from .conftest import TelegramHandlerTestFixture
from .helpers import wait_for_bot_response

logger = logging.getLogger(__name__)

# --- Test Helper Functions ---


def create_context(
    application: Application[Any, Any, Any, Any, Any, Any],
    args: list[str] | None = None,
    chat_id: int = 123,
    user_id: int = 12345,
    bot_data: dict[Any, Any] | None = None,
) -> ContextTypes.DEFAULT_TYPE:
    """Creates a CallbackContext from an Application."""
    context = ContextTypes.DEFAULT_TYPE(
        application=application, chat_id=chat_id, user_id=user_id
    )
    if bot_data:
        context.bot_data.update(bot_data)
    if args is not None:
        context.args = args
    return context


def create_mock_update(
    message_text: str,
    chat_id: int = 123,
    user_id: int = 12345,
    message_id: int = 101,
    reply_to_message: Message | None = None,
) -> Update:
    """Creates a mock Telegram Update object for a text message."""
    user = User(id=user_id, first_name="TestUser", is_bot=False)
    chat = Chat(id=chat_id, type="private")
    message = Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        text=message_text,
        reply_to_message=reply_to_message,
    )
    # Generate positive int for update_id by masking to 31 bits
    uuid_obj = uuid.uuid4()
    # basedpyright incorrectly infers UUID.int as a property descriptor rather than int
    update_id = uuid_obj.int & ((1 << 31) - 1)  # type: ignore[operator]
    update = Update(update_id=update_id, message=message)
    return update


# --- Test Cases ---


@pytest.mark.asyncio
async def test_slash_command_routes_to_specific_profile(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """
    Tests that a slash command (e.g., /focus) correctly routes to a
    ProcessingService instance configured with a specific profile,
    verified by checking the system prompt used in the LLM call.
    """
    # Arrange
    fix = telegram_handler_fixture
    user_chat_id = 123  # Use same chat_id as fixture's telegram_client
    user_id = 12345  # Changed to an authorized user ID
    user_message_id = 301

    slash_command = "/focus"
    query_text = "What is the capital of France?"
    user_command_text = f"{slash_command} {query_text}"
    command_args: list[str] = [str(s) for s in query_text.split()]

    focused_profile_id = "focused_assistant_profile"
    # Define a system prompt template for the focused profile.
    # The {current_time} placeholder will be filled by ProcessingService.
    # The matcher will check for the static part.
    focused_system_prompt_template = "You are a highly focused assistant. Your sole task is to answer questions concisely. Current time is {current_time}."
    focused_system_prompt_check_substr = "You are a highly focused assistant."

    llm_response_text = "The capital of France is Paris."

    # 1. Configure the focused profile's ProcessingService
    # Create a ProcessingServiceConfig for the focused profile
    # We only need to override prompts for this test. Other configs can be default.
    # The ProcessingServiceConfig expects a flat dict[str, str] for prompts.
    focused_prompts_config = {"system_prompt": focused_system_prompt_template}
    # For this test, the mock LLM client is shared, so system prompt is the main differentiator.
    focused_service_config = ProcessingServiceConfig(
        prompts=focused_prompts_config,
        timezone_str=fix.processing_service.service_config.timezone_str,
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={},  # Added missing tools_config
        delegation_security_level="confirm",  # Added
        id=focused_profile_id,  # Added
        # llm_model_name, llm_temperature, llm_max_tokens can be added if needed
    )

    # Define a typed AppConfig for this test context
    test_app_config_for_profile = AppConfig()

    # Create the ProcessingService instance for the focused profile
    # It will use the same mock LLM and tools provider as the default service for simplicity in this test
    focused_processing_service = ProcessingService(
        service_config=focused_service_config,
        llm_client=fix.mock_llm,  # Share the mock LLM
        tools_provider=fix.tools_provider,  # Corrected attribute name
        app_config=test_app_config_for_profile,  # Pass the test-specific app_config
        context_providers=[],  # Add missing
        server_url="http://test.server",  # Add missing
        # default_processing_profile_id and processing_services_registry are not directly used by PS itself
    )

    # 2. Manually populate the registry and slash command map on the TelegramService instance
    # This simulates how TelegramService would be initialized with multiple profiles from app_config
    fix.handler.telegram_service.processing_services_registry[focused_profile_id] = (
        focused_processing_service
    )
    fix.handler.telegram_service.slash_command_to_profile_id_map[slash_command] = (
        focused_profile_id
    )
    logger.info(
        f"Manually configured profile '{focused_profile_id}' for command '{slash_command}' in test."
    )
    logger.info(
        f"Registry: {fix.handler.telegram_service.processing_services_registry}"
    )
    logger.info(
        f"Slash map: {fix.handler.telegram_service.slash_command_to_profile_id_map}"
    )

    # 3. Define LLM rules for the mock LLM client
    def focused_profile_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        system_prompt = get_system_prompt(messages)
        last_text = get_last_message_text(messages)

        system_prompt_ok = (
            system_prompt is not None
            and focused_system_prompt_check_substr in system_prompt
        )
        last_text_ok = last_text == query_text

        logger.debug(
            f"FocusedProfileMatcher: System prompt OK? {system_prompt_ok} (Actual: '{system_prompt}'). Last text OK? {last_text_ok} (Actual: '{last_text}')"
        )
        return system_prompt_ok and last_text_ok

    llm_rule = (
        focused_profile_matcher,
        LLMResponseOutput(content=llm_response_text, tool_calls=None),
    )
    typing.cast("RuleBasedMockLLMClient", fix.mock_llm).rules = [llm_rule]

    # 4. Create mock Update and Context using the real application
    update = create_mock_update(
        user_command_text,
        chat_id=user_chat_id,
        user_id=user_id,
        message_id=user_message_id,
    )
    # Use real application from fixture, not mock
    context = create_context(
        fix.application,
        args=command_args,
        chat_id=user_chat_id,
        user_id=user_id,
        bot_data={
            "processing_service": fix.processing_service
        },  # Default service for other parts
    )

    # Act: Call the generic slash command handler directly
    # In a real scenario, PTB's CommandHandler would invoke this.
    await fix.handler.handle_generic_slash_command(update, context)

    # Assert
    with soft_assertions():  # type: ignore[attr-defined]
        # 1. LLM Call Verification
        # Ensure the LLM was called (the matcher verifies the content)
        assert_that(
            typing.cast("RuleBasedMockLLMClient", fix.mock_llm)._calls
        ).described_as("LLM Call Count").is_length(1)

        # 2. Verify bot response via telegram-test-api
        # Create a client for the specific chat_id used in this test
        test_client = fix.telegram_client
        # Note: The telegram_client is configured with a default chat_id,
        # but the bot sends to user_chat_id (456), which may not match.
        # For this test, we verify via the bot's behavior instead.

        # Wait for bot response and verify content
        bot_responses = await wait_for_bot_response(test_client, timeout=5.0)
        assert_that(bot_responses).described_as("Bot responses").is_not_empty()

        # Get the bot's message text
        bot_message = bot_responses[-1]
        bot_message_text = bot_message.get("message", {}).get("text", "")

        expected_escaped_text = telegramify_markdown.markdownify(llm_response_text)
        assert_that(bot_message_text).described_as("Bot message text").is_equal_to(
            expected_escaped_text
        )

        # 3. Confirmation Manager should not be called for this simple query
        fix.mock_confirmation_manager.request_confirmation.assert_not_awaited()
