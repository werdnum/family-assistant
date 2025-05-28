import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
import telegramify_markdown
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.context_providers import KnownUsersContextProvider
from family_assistant.interfaces import ChatInterface
from family_assistant.llm import (
    ToolCallFunction,
    ToolCallItem,
)
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage import message_history_table  # Updated import
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS as local_tool_implementations_map,
)
from family_assistant.tools import (
    TOOLS_DEFINITION as local_tools_definition_list,
)
from family_assistant.tools import (
    CompositeToolsProvider,
    ConfirmingToolsProvider,
    LocalToolsProvider,
    MCPToolsProvider,
    ToolsProvider,
)
from tests.mocks.mock_llm import (
    LLMOutput as MockLLMOutput,
)
from tests.mocks.mock_llm import (
    MatcherArgs,
    RuleBasedMockLLMClient,
    get_last_message_text,
)

logger = logging.getLogger(__name__)

# --- Test Constants ---
PRIMARY_PROFILE_ID = "primary_delegator"
SPECIALIZED_PROFILE_ID = "specialized_target"
DELEGATED_TASK_DESCRIPTION = "Solve this complex problem for me."
USER_QUERY_TEMPLATE = "Please delegate this task: {task_description}"

TEST_CHAT_ID = 123456789  # Changed to an integer
TEST_INTERFACE_TYPE = "test_interface"
TEST_USER_NAME = "DelegationTester"

# --- Fixtures ---


@pytest.fixture
def dummy_prompts() -> dict[str, str]:
    return {"system_prompt": "You are a {profile_id} assistant."}


@pytest.fixture
def primary_service_config(dummy_prompts: dict[str, str]) -> ProcessingServiceConfig:
    return ProcessingServiceConfig(
        prompts=dummy_prompts,
        calendar_config={},
        timezone_str="UTC",
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={
            "enable_local_tools": ["delegate_to_service"],  # Crucial for this test
            "confirm_tools": [],
        },
        delegation_security_level="unrestricted",  # Primary can delegate freely
        id=PRIMARY_PROFILE_ID,
    )


@pytest.fixture
def specialized_service_config_factory(
    dummy_prompts: dict[str, str],
) -> Callable[[str], ProcessingServiceConfig]:
    def _factory(delegation_security_level: str) -> ProcessingServiceConfig:
        return ProcessingServiceConfig(
            prompts=dummy_prompts,
            calendar_config={},
            timezone_str="UTC",
            max_history_messages=5,
            history_max_age_hours=24,
            tools_config={  # Target profile might have its own tools or none
                "enable_local_tools": [],
                "confirm_tools": [],
            },
            delegation_security_level=delegation_security_level,
            id=SPECIALIZED_PROFILE_ID,  # Add id for specialized profile
        )

    return _factory


@pytest.fixture
def primary_llm_mock_factory() -> Callable[[bool | None], RuleBasedMockLLMClient]:
    def _factory(confirm_delegation_arg: bool | None) -> RuleBasedMockLLMClient:
        rules = []

        # Rule 1: Match the tool response from delegate_to_service
        def delegate_tool_response_matcher(kwargs: MatcherArgs) -> bool:
            messages = kwargs.get("messages", [])
            logger.debug(
                f"delegate_tool_response_matcher: checking messages: {messages}"
            )
            if not messages:
                logger.debug(
                    "delegate_tool_response_matcher: no messages, returning False"
                )
                return False
            last_message = messages[-1]
            is_tool_role = last_message.get("role") == "tool"
            content = last_message.get("content", "")
            expected_prefix = f"Response from {SPECIALIZED_PROFILE_ID}"
            starts_with_prefix = content.startswith(expected_prefix)

            match_result = is_tool_role and starts_with_prefix
            logger.debug(
                f"delegate_tool_response_matcher: last_message_role='{last_message.get('role')}', is_tool_role={is_tool_role}"
            )
            logger.debug(
                f"delegate_tool_response_matcher: content='{content[:100]}...', expected_prefix='{expected_prefix}', starts_with_prefix={starts_with_prefix}"
            )
            logger.debug(f"delegate_tool_response_matcher: returning {match_result}")
            return match_result

        def delegate_tool_final_response_callable(kwargs: MatcherArgs) -> MockLLMOutput:
            messages = kwargs.get("messages", [])
            tool_response_content = messages[-1].get(
                "content", "Error: Could not extract tool response."
            )
            logger.info(
                f"delegate_tool_final_response_callable: Matched! Returning content: {tool_response_content[:100]}..."
            )
            return MockLLMOutput(content=tool_response_content, tool_calls=None)

        rules.append((
            delegate_tool_response_matcher,
            delegate_tool_final_response_callable,
        ))

        # Rule 2: Match "delegation cancelled" tool response
        def cancelled_matcher(kwargs: MatcherArgs) -> bool:
            messages = kwargs.get("messages", [])
            logger.debug(f"cancelled_matcher: checking messages: {messages}")
            if not messages:
                logger.debug("cancelled_matcher: no messages, returning False")
                return False
            last_message = messages[-1]
            match = last_message.get(
                "role"
            ) == "tool" and "cancelled by user" in last_message.get("content", "")
            logger.debug(
                f"cancelled_matcher: returning {match} for content: '{last_message.get('content', '')[:100]}...'"
            )
            return match

        def cancelled_response_callable(kwargs: MatcherArgs) -> MockLLMOutput:
            messages = kwargs.get("messages", [])
            content = messages[-1].get(
                "content", "Error: Could not get cancelled content."
            )
            logger.info(
                f"cancelled_response_callable: Matched! Returning content: {content[:100]}..."
            )
            return MockLLMOutput(content=content)

        rules.append((cancelled_matcher, cancelled_response_callable))

        # Rule 3: Match "delegation blocked" tool response
        def blocked_matcher(kwargs: MatcherArgs) -> bool:
            messages = kwargs.get("messages", [])
            logger.debug(f"blocked_matcher: checking messages: {messages}")
            if not messages:
                logger.debug("blocked_matcher: no messages, returning False")
                return False
            last_message = messages[-1]
            content_str = last_message.get("content", "")
            # Make the match more specific to the exact error message
            expected_error_message = f"Error: Delegation to service profile '{SPECIALIZED_PROFILE_ID}' is not allowed."
            match = (
                last_message.get("role") == "tool"
                and content_str == expected_error_message
            )
            logger.debug(
                f"blocked_matcher: checking content='{content_str[:100]}...' against expected='{expected_error_message}'. Match: {match}"
            )
            return match

        def blocked_response_callable(kwargs: MatcherArgs) -> MockLLMOutput:
            messages = kwargs.get("messages", [])
            # Ensure we return the exact content that was matched
            content = messages[
                -1
            ].get(
                "content",
                f"Error: Delegation to service profile '{SPECIALIZED_PROFILE_ID}' is not allowed.",  # Default to expected if somehow missing
            )
            logger.info(
                f"blocked_response_callable: Matched! Returning content: {content[:100]}..."
            )
            return MockLLMOutput(content=content)

        rules.append((blocked_matcher, blocked_response_callable))

        # Rule 4: Match initial user query to delegate
        def delegate_request_matcher(kwargs: MatcherArgs) -> bool:
            messages = kwargs.get("messages", [])
            logger.debug(f"delegate_request_matcher: checking messages: {messages}")
            if not messages:
                logger.debug("delegate_request_matcher: no messages, returning False")
                return False

            last_message_role = messages[-1].get("role")
            if last_message_role != "user":
                logger.debug(
                    f"delegate_request_matcher: last message role is '{last_message_role}', not 'user'. Returning False."
                )
                return False

            last_text = get_last_message_text(messages).lower()
            desc_in_text = DELEGATED_TASK_DESCRIPTION.lower() in last_text
            delegate_task_in_text = "delegate this task" in last_text

            match_result = desc_in_text and delegate_task_in_text
            logger.debug(
                f"delegate_request_matcher: last_text='{last_text[:100]}...', desc_in_text={desc_in_text}, delegate_task_in_text={delegate_task_in_text}"
            )
            logger.debug(f"delegate_request_matcher: returning {match_result}")
            return match_result

        # Using a callable for the response to make call_id dynamic and log match
        def delegate_request_response_callable(kwargs: MatcherArgs) -> MockLLMOutput:
            logger.info(
                "delegate_request_response_callable: Matched! Returning delegate tool call."
            )
            current_tool_call_args: dict[str, Any] = {
                "target_service_id": SPECIALIZED_PROFILE_ID,
                "user_request": DELEGATED_TASK_DESCRIPTION,
            }
            if confirm_delegation_arg is not None:
                current_tool_call_args["confirm_delegation"] = confirm_delegation_arg

            return MockLLMOutput(
                content=f"Okay, I will delegate '{DELEGATED_TASK_DESCRIPTION}' to {SPECIALIZED_PROFILE_ID}.",
                tool_calls=[
                    ToolCallItem(
                        id=f"call_dyn_{uuid.uuid4()}",
                        type="function",
                        function=ToolCallFunction(
                            name="delegate_to_service",
                            arguments=json.dumps(current_tool_call_args),
                        ),
                    )
                ],
            )

        rules.append((delegate_request_matcher, delegate_request_response_callable))

        return RuleBasedMockLLMClient(rules=rules)

    return _factory


@pytest.fixture
def specialized_llm_mock() -> RuleBasedMockLLMClient:
    # Rule: Match the delegated task description and provide a specific response
    def specialized_task_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        last_text = get_last_message_text(messages).lower()
        # This matcher expects the system prompt of the specialized agent to be prepended
        # or for the user_request to be directly in the last message.
        return DELEGATED_TASK_DESCRIPTION.lower() in last_text

    specialized_response = MockLLMOutput(
        content=f"Response from {SPECIALIZED_PROFILE_ID}: Task '{DELEGATED_TASK_DESCRIPTION}' processed.",
        tool_calls=None,
    )
    return RuleBasedMockLLMClient(
        rules=[(specialized_task_matcher, specialized_response)]
    )


@pytest_asyncio.fixture
async def mock_confirmation_callback() -> AsyncMock:
    return AsyncMock(spec=Callable[..., Awaitable[bool]])


def create_tools_provider(profile_tools_config: dict[str, Any]) -> ToolsProvider:
    """Helper to create a ToolsProvider stack for a profile."""
    enabled_local_tool_names = set(profile_tools_config.get("enable_local_tools", []))

    # If empty, enable all known local tools for simplicity in test setup,
    # or be specific if the test requires it. For delegation, primary needs delegate_to_service.
    if (
        not enabled_local_tool_names
        and "delegate_to_service" in profile_tools_config.get("enable_local_tools", [])
    ):
        enabled_local_tool_names = {"delegate_to_service"}  # Ensure primary has it
    elif (
        not enabled_local_tool_names
    ):  # For specialized, if empty, means no local tools
        pass

    profile_local_definitions = [
        td
        for td in local_tools_definition_list
        if td.get("function", {}).get("name") in enabled_local_tool_names
    ]
    profile_local_implementations = {
        name: func
        for name, func in local_tool_implementations_map.items()
        if name in enabled_local_tool_names
    }

    logger.info(f"create_tools_provider: profile_tools_config={profile_tools_config}")
    logger.info(
        f"create_tools_provider: enabled_local_tool_names={enabled_local_tool_names}"
    )
    logger.info(
        f"create_tools_provider: profile_local_definitions names={[d.get('function', {}).get('name') for d in profile_local_definitions]}"
    )
    logger.info(
        f"create_tools_provider: profile_local_implementations keys={list(profile_local_implementations.keys())}"
    )

    local_provider = LocalToolsProvider(
        definitions=profile_local_definitions,
        implementations=profile_local_implementations,
    )
    mcp_provider = MCPToolsProvider(mcp_server_configs={})  # Mocked

    composite_provider = CompositeToolsProvider(
        providers=[local_provider, mcp_provider]
    )

    confirm_tools_set = set(profile_tools_config.get("confirm_tools", []))
    confirming_provider = ConfirmingToolsProvider(
        wrapped_provider=composite_provider,
        tools_requiring_confirmation=confirm_tools_set,
    )
    return confirming_provider


@pytest_asyncio.fixture
async def primary_processing_service(
    primary_service_config: ProcessingServiceConfig,
    primary_llm_mock_factory: Callable[[bool | None], RuleBasedMockLLMClient],
    dummy_prompts: dict[str, str],
) -> ProcessingService:
    # Default to no confirm_delegation argument for the primary LLM's tool call
    llm_mock = primary_llm_mock_factory(None)
    tools_provider = create_tools_provider(primary_service_config.tools_config)
    await tools_provider.get_tool_definitions()  # Initialize

    known_users_provider = KnownUsersContextProvider(
        chat_id_to_name_map={TEST_CHAT_ID: TEST_USER_NAME}, prompts=dummy_prompts
    )

    return ProcessingService(
        llm_client=llm_mock,
        tools_provider=tools_provider,
        service_config=primary_service_config,
        context_providers=[known_users_provider],
        server_url="http://test.server",
        app_config={},
    )


@pytest_asyncio.fixture
async def specialized_processing_service(
    specialized_service_config_factory: Callable[[str], ProcessingServiceConfig],
    specialized_llm_mock: RuleBasedMockLLMClient,
    dummy_prompts: dict[str, str],
) -> Callable[[str], Awaitable[ProcessingService]]:
    async def _factory(delegation_security_level: str) -> ProcessingService:
        config = specialized_service_config_factory(delegation_security_level)
        tools_provider = create_tools_provider(config.tools_config)
        await tools_provider.get_tool_definitions()  # Initialize

        known_users_provider = KnownUsersContextProvider(
            chat_id_to_name_map={TEST_CHAT_ID: TEST_USER_NAME},
            prompts=dummy_prompts,
        )

        return ProcessingService(
            llm_client=specialized_llm_mock,
            tools_provider=tools_provider,
            service_config=config,
            context_providers=[known_users_provider],
            server_url="http://test.server",
            app_config={},
        )

    return _factory


async def assert_message_history_contains(
    db_context: DatabaseContext,
    conversation_id: str,
    expected_role: str,
    expected_content_substring: str | None = None,
    expected_tool_call_name: str | None = None,
    min_messages: int = 1,
) -> None:
    history = await db_context.fetch_all(
        select(message_history_table)
        .where(message_history_table.c.conversation_id == conversation_id)
        .order_by(message_history_table.c.timestamp.asc())
    )
    assert len(history) >= min_messages, (
        f"Expected at least {min_messages} messages, found {len(history)}"
    )

    found_match = False
    for msg in history:
        role_match = msg["role"] == expected_role  # Use dictionary access
        content_match = True
        if expected_content_substring:
            content_match = (
                msg["content"]  # Use dictionary access
                and expected_content_substring.lower()
                in msg["content"].lower()  # Use dictionary access
            )

        tool_call_match = True
        if expected_tool_call_name:
            tool_calls = msg["tool_calls"]  # Use dictionary access
            if isinstance(tool_calls, list) and tool_calls:
                tool_call_match = any(
                    tc.get("function", {}).get("name") == expected_tool_call_name
                    for tc in tool_calls
                )
            else:
                tool_call_match = False

        if role_match and content_match and tool_call_match:
            found_match = True
            break

    assert found_match, (
        f"Message with role '{expected_role}', content containing '{expected_content_substring}', and tool call '{expected_tool_call_name}' not found."
    )


# --- Test Cases ---


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "confirm_tool_arg", [False, None]
)  # Test with confirm_delegation=False and when arg is omitted
async def test_delegation_unrestricted_target_no_forced_confirm(
    test_db_engine: AsyncEngine,
    primary_processing_service: Awaitable[
        ProcessingService
    ],  # Uses primary_llm_mock_factory(None) by default
    specialized_processing_service: Awaitable[
        Callable[[str], Awaitable[ProcessingService]]
    ],
    mock_confirmation_callback: Awaitable[AsyncMock],
    primary_llm_mock_factory: Callable[[bool | None], RuleBasedMockLLMClient],
    confirm_tool_arg: bool | None,
) -> None:
    """Target is 'unrestricted', tool call confirm_delegation is False or omitted. Expect no confirmation."""
    logger.info("--- Test: Unrestricted Target, No Forced Confirmation ---")

    # Await fixtures to get their resolved values
    awaited_primary_service = await primary_processing_service
    awaited_mock_confirmation_callback = await mock_confirmation_callback
    awaited_specialized_processing_service_factory = (
        await specialized_processing_service
    )

    # Reconfigure primary LLM mock for this specific tool argument
    awaited_primary_service.llm_client = primary_llm_mock_factory(confirm_tool_arg)

    target_service = await awaited_specialized_processing_service_factory(
        "unrestricted"
    )

    registry = {
        PRIMARY_PROFILE_ID: awaited_primary_service,
        SPECIALIZED_PROFILE_ID: target_service,
    }
    awaited_primary_service.set_processing_services_registry(registry)
    target_service.set_processing_services_registry(registry)

    user_query = USER_QUERY_TEMPLATE.format(task_description=DELEGATED_TASK_DESCRIPTION)

    async with DatabaseContext(engine=test_db_engine) as db_context:
        (
            final_reply,
            _,
            _,
            error,
        ) = await awaited_primary_service.handle_chat_interaction(
            db_context=db_context,
            interface_type=TEST_INTERFACE_TYPE,
            conversation_id=str(TEST_CHAT_ID),  # Ensure conversation_id is string
            trigger_content_parts=[{"type": "text", "text": user_query}],
            trigger_interface_message_id="msg1",
            user_name=TEST_USER_NAME,
            chat_interface=MagicMock(spec=ChatInterface),
            new_task_event=asyncio.Event(),
            request_confirmation_callback=awaited_mock_confirmation_callback,
        )

    assert error is None, f"Error during interaction: {error}"
    assert final_reply is not None
    assert f"Response from {SPECIALIZED_PROFILE_ID}" in final_reply
    assert DELEGATED_TASK_DESCRIPTION in final_reply
    awaited_mock_confirmation_callback.assert_not_called()

    # DB Assertions
    async with DatabaseContext(engine=test_db_engine) as db_context:
        await assert_message_history_contains(
            db_context, str(TEST_CHAT_ID), "user", user_query
        )
        await assert_message_history_contains(
            db_context, str(TEST_CHAT_ID), "assistant", None, "delegate_to_service"
        )
        # Check for the specialized service's response being part of the tool result for delegate_to_service
        # This is a bit indirect. The final assistant message from primary should contain it.
        await assert_message_history_contains(
            db_context,
            str(TEST_CHAT_ID),
            "assistant",
            f"Response from {SPECIALIZED_PROFILE_ID}",
        )


@pytest.mark.asyncio
async def test_delegation_confirm_target_granted(
    test_db_engine: AsyncEngine,
    primary_processing_service: Awaitable[
        ProcessingService
    ],  # Uses primary_llm_mock_factory(None) by default
    specialized_processing_service: Awaitable[
        Callable[[str], Awaitable[ProcessingService]]
    ],
    mock_confirmation_callback: Awaitable[AsyncMock],
    primary_llm_mock_factory: Callable[[bool | None], RuleBasedMockLLMClient],
) -> None:
    """Target is 'confirm', tool confirm_delegation=False. Expect confirmation, user grants it."""
    logger.info("--- Test: Confirm Target, Confirmation Granted ---")

    # Await fixtures
    awaited_primary_service = await primary_processing_service
    awaited_mock_confirmation_callback = await mock_confirmation_callback
    awaited_specialized_processing_service_factory = (
        await specialized_processing_service
    )

    awaited_primary_service.llm_client = primary_llm_mock_factory(
        False
    )  # Explicitly set confirm_delegation=False
    awaited_mock_confirmation_callback.return_value = True  # User confirms

    target_service = await awaited_specialized_processing_service_factory("confirm")

    registry = {
        PRIMARY_PROFILE_ID: awaited_primary_service,
        SPECIALIZED_PROFILE_ID: target_service,
    }
    awaited_primary_service.set_processing_services_registry(registry)
    target_service.set_processing_services_registry(registry)

    user_query = USER_QUERY_TEMPLATE.format(task_description=DELEGATED_TASK_DESCRIPTION)

    async with DatabaseContext(engine=test_db_engine) as db_context:
        (
            final_reply,
            _,
            _,
            error,
        ) = await awaited_primary_service.handle_chat_interaction(
            db_context=db_context,
            interface_type=TEST_INTERFACE_TYPE,
            conversation_id=str(TEST_CHAT_ID),  # Ensure conversation_id is string
            trigger_content_parts=[{"type": "text", "text": user_query}],
            trigger_interface_message_id="msg2",
            user_name=TEST_USER_NAME,
            chat_interface=MagicMock(spec=ChatInterface),
            new_task_event=asyncio.Event(),
            request_confirmation_callback=awaited_mock_confirmation_callback,
        )

    assert error is None, f"Error during interaction: {error}"
    assert final_reply is not None
    assert f"Response from {SPECIALIZED_PROFILE_ID}" in final_reply
    awaited_mock_confirmation_callback.assert_called_once()
    # Assert call args for confirmation if needed (tool_name, specific prompt text)
    call_args = awaited_mock_confirmation_callback.call_args[1]  # kwargs of the call
    assert call_args["tool_name"] == "delegate_to_service"
    # Compare with the escaped version of the description
    escaped_description = telegramify_markdown.escape_markdown(
        DELEGATED_TASK_DESCRIPTION
    )
    assert escaped_description.lower() in call_args["prompt_text"].lower()
    escaped_profile_id = telegramify_markdown.escape_markdown(SPECIALIZED_PROFILE_ID)
    assert escaped_profile_id.lower() in call_args["prompt_text"].lower()


@pytest.mark.asyncio
async def test_delegation_confirm_target_denied(
    test_db_engine: AsyncEngine,
    primary_processing_service: Awaitable[ProcessingService],
    specialized_processing_service: Awaitable[
        Callable[[str], Awaitable[ProcessingService]]
    ],
    mock_confirmation_callback: Awaitable[AsyncMock],
    primary_llm_mock_factory: Callable[[bool | None], RuleBasedMockLLMClient],
) -> None:
    """Target is 'confirm', user denies confirmation."""
    logger.info("--- Test: Confirm Target, Confirmation Denied ---")

    # Await fixtures
    awaited_primary_service = await primary_processing_service
    awaited_mock_confirmation_callback = await mock_confirmation_callback
    awaited_specialized_processing_service_factory = (
        await specialized_processing_service
    )

    awaited_primary_service.llm_client = primary_llm_mock_factory(False)
    awaited_mock_confirmation_callback.return_value = False  # User denies

    target_service = await awaited_specialized_processing_service_factory("confirm")

    registry = {
        PRIMARY_PROFILE_ID: awaited_primary_service,
        SPECIALIZED_PROFILE_ID: target_service,
    }
    awaited_primary_service.set_processing_services_registry(registry)
    target_service.set_processing_services_registry(registry)

    user_query = USER_QUERY_TEMPLATE.format(task_description=DELEGATED_TASK_DESCRIPTION)

    async with DatabaseContext(engine=test_db_engine) as db_context:
        (
            final_reply,
            _,
            _,
            error,
        ) = await awaited_primary_service.handle_chat_interaction(
            db_context=db_context,
            interface_type=TEST_INTERFACE_TYPE,
            conversation_id=str(TEST_CHAT_ID),  # Ensure conversation_id is string
            trigger_content_parts=[{"type": "text", "text": user_query}],
            trigger_interface_message_id="msg3",
            user_name=TEST_USER_NAME,
            chat_interface=MagicMock(spec=ChatInterface),
            new_task_event=asyncio.Event(),
            request_confirmation_callback=awaited_mock_confirmation_callback,
        )

    assert error is None, f"Error during interaction: {error}"
    assert final_reply is not None
    assert "delegation to service" in final_reply.lower()
    assert "cancelled by user" in final_reply.lower()
    assert (
        f"Response from {SPECIALIZED_PROFILE_ID}" not in final_reply
    )  # Specialized service should not be called
    awaited_mock_confirmation_callback.assert_called_once()


@pytest.mark.asyncio
async def test_delegation_blocked_target(
    test_db_engine: AsyncEngine,
    primary_processing_service: Awaitable[ProcessingService],
    specialized_processing_service: Awaitable[
        Callable[[str], Awaitable[ProcessingService]]
    ],
    mock_confirmation_callback: Awaitable[AsyncMock],
) -> None:
    """Target is 'blocked'. Expect delegation to fail."""
    logger.info("--- Test: Blocked Target ---")

    # Await fixtures
    awaited_primary_service = await primary_processing_service
    awaited_mock_confirmation_callback = await mock_confirmation_callback
    awaited_specialized_processing_service_factory = (
        await specialized_processing_service
    )
    # Primary LLM mock will attempt to delegate (confirm_delegation arg doesn't matter here)

    target_service = await awaited_specialized_processing_service_factory("blocked")

    registry = {
        PRIMARY_PROFILE_ID: awaited_primary_service,
        SPECIALIZED_PROFILE_ID: target_service,
    }
    awaited_primary_service.set_processing_services_registry(registry)
    target_service.set_processing_services_registry(registry)

    user_query = USER_QUERY_TEMPLATE.format(task_description=DELEGATED_TASK_DESCRIPTION)

    async with DatabaseContext(engine=test_db_engine) as db_context:
        (
            final_reply,
            _,
            _,
            error,
        ) = await awaited_primary_service.handle_chat_interaction(
            db_context=db_context,
            interface_type=TEST_INTERFACE_TYPE,
            conversation_id=str(TEST_CHAT_ID),  # Ensure conversation_id is string
            trigger_content_parts=[{"type": "text", "text": user_query}],
            trigger_interface_message_id="msg4",
            user_name=TEST_USER_NAME,
            chat_interface=MagicMock(spec=ChatInterface),
            new_task_event=asyncio.Event(),
            request_confirmation_callback=awaited_mock_confirmation_callback,
        )

    assert error is None, f"Error during interaction: {error}"
    assert final_reply is not None
    assert "error: delegation to service profile" in final_reply.lower()
    assert "not allowed" in final_reply.lower()
    assert f"Response from {SPECIALIZED_PROFILE_ID}" not in final_reply
    awaited_mock_confirmation_callback.assert_not_called()  # Confirmation should not even be attempted


@pytest.mark.asyncio
async def test_delegation_unrestricted_confirm_arg_granted(
    test_db_engine: AsyncEngine,
    primary_processing_service: Awaitable[
        ProcessingService
    ],  # Uses primary_llm_mock_factory(None) by default
    specialized_processing_service: Awaitable[
        Callable[[str], Awaitable[ProcessingService]]
    ],
    mock_confirmation_callback: Awaitable[AsyncMock],
    primary_llm_mock_factory: Callable[[bool | None], RuleBasedMockLLMClient],
) -> None:
    """Target is 'unrestricted', tool call confirm_delegation=True. Expect confirmation, user grants."""
    logger.info("--- Test: Unrestricted Target, Confirm Argument True, Granted ---")

    # Await fixtures
    awaited_primary_service = await primary_processing_service
    awaited_mock_confirmation_callback = await mock_confirmation_callback
    awaited_specialized_processing_service_factory = (
        await specialized_processing_service
    )

    awaited_primary_service.llm_client = primary_llm_mock_factory(
        True
    )  # confirm_delegation=True in tool call
    awaited_mock_confirmation_callback.return_value = True  # User confirms

    target_service = await awaited_specialized_processing_service_factory(
        "unrestricted"
    )

    registry = {
        PRIMARY_PROFILE_ID: awaited_primary_service,
        SPECIALIZED_PROFILE_ID: target_service,
    }
    awaited_primary_service.set_processing_services_registry(registry)
    target_service.set_processing_services_registry(registry)

    user_query = USER_QUERY_TEMPLATE.format(task_description=DELEGATED_TASK_DESCRIPTION)

    async with DatabaseContext(engine=test_db_engine) as db_context:
        (
            final_reply,
            _,
            _,
            error,
        ) = await awaited_primary_service.handle_chat_interaction(
            db_context=db_context,
            interface_type=TEST_INTERFACE_TYPE,
            conversation_id=str(TEST_CHAT_ID),  # Ensure conversation_id is string
            trigger_content_parts=[{"type": "text", "text": user_query}],
            trigger_interface_message_id="msg5",
            user_name=TEST_USER_NAME,
            chat_interface=MagicMock(spec=ChatInterface),
            new_task_event=asyncio.Event(),
            request_confirmation_callback=awaited_mock_confirmation_callback,
        )

    assert error is None, f"Error during interaction: {error}"
    assert final_reply is not None
    assert f"Response from {SPECIALIZED_PROFILE_ID}" in final_reply
    awaited_mock_confirmation_callback.assert_called_once()
    # Assert that the tool_args in the confirmation call reflect confirm_delegation=True
    confirmed_tool_args = awaited_mock_confirmation_callback.call_args[1]["tool_args"]
    assert confirmed_tool_args.get("confirm_delegation") is True
