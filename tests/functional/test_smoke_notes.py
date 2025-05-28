import asyncio
import json  # Added json import
import logging
import uuid  # Added for turn_id
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text  # To query DB directly for assertion
from sqlalchemy.ext.asyncio import AsyncEngine  # Added for type hints

# Import ContextProvider and NotesContextProvider
from family_assistant.context_providers import (
    NotesContextProvider,
)

if TYPE_CHECKING:
    from family_assistant.llm import LLMInterface  # Keep Interface

# Import necessary classes for instantiation
# _generate_llm_response_for_chat was moved to ProcessingService
# Import DatabaseContext and getter
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS as local_tool_implementations,
)
from family_assistant.tools import (
    TOOLS_DEFINITION as local_tools_definition,
)
from family_assistant.tools import (
    CompositeToolsProvider,
    LocalToolsProvider,
    MCPToolsProvider,
)

# Import the rule-based mock
from tests.mocks.mock_llm import (
    LLMOutput as MockLLMOutput,  # Import the mock's LLMOutput
)
from tests.mocks.mock_llm import (
    MatcherArgs,  # Added import
    Rule,
    RuleBasedMockLLMClient,
    get_last_message_text,
)

logger = logging.getLogger(__name__)

# --- Test Configuration ---
# Use a unique title for each test run to avoid collisions
TEST_NOTE_TITLE_BASE = "Smoketest Note"
TEST_NOTE_CONTENT = "This is the content for the smoke test."
TEST_CHAT_ID = 12345  # Dummy chat ID
TEST_USER_ID = 98765  # Added User ID
TEST_USER_NAME = "NotesTestUser"
# TEST_MODEL_NAME is no longer needed for the mock


@pytest.mark.asyncio
async def test_add_and_retrieve_note_rule_mock(
    test_db_engine: AsyncEngine,
) -> None:  # Renamed test
    """
    Rule-based mock test:
    1. Define rules for adding and retrieving a specific note.
    2. Instantiate RuleBasedMockLLMClient with these rules.
    3. Simulate adding a note, triggering the 'add' rule and tool call.
    4. Verify the note exists in the database.
    5. Simulate asking about the note, triggering the 'retrieve' rule.
    6. Verify the mock's response includes the note content without a tool call.
    """
    # --- Setup ---
    user_message_id_add = 171  # Message ID for adding the note
    user_message_id_retrieve = 235  # Message ID for retrieving the note
    test_note_title = f"{TEST_NOTE_TITLE_BASE} {uuid.uuid4()}"
    test_tool_call_id = f"call_{uuid.uuid4()}"  # Pre-generate ID for the rule
    logger.info("\n--- Running Rule-Based Mock Test: Add/Retrieve Note ---")
    logger.info(f"Using Note Title: {test_note_title}")

    # --- Define Rules ---

    # Rule 1: Match Add Note Request
    def add_note_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        tools = kwargs.get("tools")

        last_text = get_last_message_text(messages).lower()
        return (
            "remember this note" in last_text
            and f"title: {test_note_title}".lower() in last_text
            and f"content: {TEST_NOTE_CONTENT}".lower() in last_text
            and tools is not None  # Check that tools were actually provided
        )

    add_note_response = MockLLMOutput(  # Use the mock's LLMOutput
        content="OK, I will add that note via the rule-based mock.",
        tool_calls=[
            ToolCallItem(
                id=test_tool_call_id,  # Use pre-generated ID
                type="function",
                function=ToolCallFunction(
                    name="add_or_update_note",
                    arguments=json.dumps({  # Arguments must be JSON string
                        "title": test_note_title,
                        "content": TEST_NOTE_CONTENT,
                    }),
                ),
            )
        ],
    )
    add_note_rule: Rule = (add_note_matcher, add_note_response)

    # Rule 2: Match Retrieve Note Request
    def retrieve_note_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])

        last_text = get_last_message_text(messages).lower()
        # NOTE: This simple rule-based mock is stateless.
        # It doesn't know if the note was *actually* added before.
        # We rely on the test structure to call add before retrieve.
        return (
            "what do you know about" in last_text
            and f"note titled '{test_note_title}'".lower() in last_text
        )

    retrieve_note_response = MockLLMOutput(  # Use the mock's LLMOutput
        content=f"Rule-based mock says: The note '{test_note_title}' contains: {TEST_NOTE_CONTENT}",
        tool_calls=None,  # No tool call for retrieval
    )
    retrieve_note_rule: Rule = (retrieve_note_matcher, retrieve_note_response)

    # --- Instantiate Mock LLM ---
    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[add_note_rule, retrieve_note_rule]
        # Can optionally provide a specific default_response here
    )
    logger.info("Using RuleBasedMockLLMClient for testing.")

    # --- Instantiate other dependencies (Tool Providers remain the same) ---
    local_provider = LocalToolsProvider(
        definitions=local_tools_definition, implementations=local_tool_implementations
    )
    # Mock MCP provider as it's not needed for this test
    mcp_provider = MCPToolsProvider(mcp_server_configs={})  # Use correct argument name
    composite_provider = CompositeToolsProvider(
        providers=[local_provider, mcp_provider]
    )
    # Eagerly fetch definitions (optional in test, but good practice)
    await composite_provider.get_tool_definitions()

    # Processing Service - Add dummy config values needed by the new __init__
    dummy_prompts = {"system_prompt": "Test system prompt."}
    dummy_calendar_config = {}
    dummy_timezone_str = "UTC"
    dummy_max_history = 5
    dummy_history_age = 24
    dummy_app_config = {}  # Add dummy app_config

    # --- Instantiate Context Providers ---
    # Function to get DB context for the specific test engine.
    # This function will be called by NotesContextProvider.
    # It returns an "active" DatabaseContext instance by calling __aenter__().
    # This matches the expected type Callable[[], Awaitable[DatabaseContext]].
    # Note: This pattern implies that NotesContextProvider does not manage
    # the __aexit__ part of the context, which could lead to resource leaks
    # if not handled carefully (e.g. if NotesContextProvider calls this many times).
    # For this test, we assume this is acceptable to satisfy the type hints.
    async def get_test_db_context_func() -> DatabaseContext:
        manager = get_db_context(engine=test_db_engine)
        return await manager.__aenter__()

    notes_provider = NotesContextProvider(
        get_db_context_func=get_test_db_context_func,
        prompts=dummy_prompts,
    )

    # --- Create ServiceConfig ---
    test_service_config_obj_notes = ProcessingServiceConfig(
        prompts=dummy_prompts,
        calendar_config=dummy_calendar_config,
        timezone_str=dummy_timezone_str,
        max_history_messages=dummy_max_history,
        history_max_age_hours=dummy_history_age,
        tools_config={},  # Added missing tools_config
        delegation_security_level="confirm",  # Added
        id="test_smoke_notes_profile",  # Added
    )

    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        context_providers=[notes_provider],  # Pass the notes provider
        service_config=test_service_config_obj_notes,  # Use renamed variable
        server_url=None,  # Added server_url
        app_config=dummy_app_config,  # Added app_config
    )
    logger.info(
        f"Instantiated ProcessingService with {type(llm_client).__name__}, {type(composite_provider).__name__}, service_config, server_url, and app_config"
    )

    # mock_application is no longer needed for ToolExecutionContext

    # --- Part 1: Add the note ---
    add_note_text = f"Please remember this note. Title: {test_note_title}. Content: {TEST_NOTE_CONTENT}"
    add_note_trigger = [{"type": "text", "text": add_note_text}]

    # Create a DatabaseContext using the test engine provided by the fixture
    # Note: test_db_engine fixture comes from the root conftest.py
    async with DatabaseContext(engine=test_db_engine) as db_context:
        # Call the method on the ProcessingService instance
        # Unpack the 4 return values correctly
        (
            add_final_text_reply,
            _add_final_assistant_msg_id,  # Not used here
            _add_reasoning_info,  # Not used here
            add_error,
        ) = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            new_task_event=asyncio.Event(),
            interface_type="test",  # Added interface type
            conversation_id=str(TEST_CHAT_ID),  # Added conversation ID as string
            # turn_id is generated by handle_chat_interaction
            trigger_content_parts=add_note_trigger,
            trigger_interface_message_id=str(
                user_message_id_add
            ),  # Added missing argument
            user_name=TEST_USER_NAME,
        )
    # Assertions remain outside the context manager
    assert add_error is None, f"Error during add note: {add_error}"
    assert add_final_text_reply is not None, (
        "No final text reply generated during add note turn"
    )

    # Assertion 1: Check the database directly to confirm the note was added
    # The tool call itself is now an internal detail of handle_chat_interaction,
    # but we still expect the note to be in the DB if the LLM rule for tool call was matched.
    # We can't directly inspect `add_turn_messages` for the tool call as it's not returned.
    # We rely on the LLM mock rule being correct and the tool execution succeeding.

    note_in_db = None

    logger.info("Checking database for the new note...")
    async with test_db_engine.connect() as connection:  # Correct indentation
        result = await connection.execute(
            text("SELECT title, content FROM notes WHERE title = :title"),
            {"title": test_note_title},
        )
        note_in_db = result.fetchone()

    assert note_in_db is not None, (
        f"Note '{test_note_title}' not found in the database after rule-based mock add attempt."
    )
    assert note_in_db.content == TEST_NOTE_CONTENT, "Note content in DB does not match."
    logger.info("Tool info check passed.")

    # --- Add a small delay ---
    await asyncio.sleep(0.1)  # Can be shorter with mock

    logger.info("\n--- Running Rule-Based Mock Test: Retrieve Note ---")
    # --- Part 2: Retrieve the note ---
    retrieve_note_text = f"What do you know about the note titled '{test_note_title}'?"
    retrieve_note_trigger = [{"type": "text", "text": retrieve_note_text}]

    # Create a new context for the retrieval part (or reuse if appropriate, but new is safer for isolation)
    async with DatabaseContext(engine=test_db_engine) as db_context:
        # Call the method on the ProcessingService instance again
        (
            retrieve_final_text_reply,
            _retrieve_final_assistant_msg_id,  # Not used
            _retrieve_final_reasoning_info,  # Not used
            retrieve_error,
        ) = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            new_task_event=asyncio.Event(),
            interface_type="test",  # Added missing interface type
            conversation_id=str(TEST_CHAT_ID),  # Added missing conversation ID
            # turn_id is generated by handle_chat_interaction
            trigger_content_parts=retrieve_note_trigger,
            trigger_interface_message_id=str(
                user_message_id_retrieve
            ),  # Added missing argument
            user_name=TEST_USER_NAME,
        )

        # model_name argument removed
    assert retrieve_error is None, (
        f"Error during retrieve note: {retrieve_error}"
    )  # Check retrieve_error
    assert retrieve_final_text_reply is not None, (
        "No final text reply generated during retrieve note turn"
    )

    # Assertion 3: Check the final response content from the mock rule
    # Use lower() for case-insensitive comparison
    assert TEST_NOTE_CONTENT.lower() in retrieve_final_text_reply.lower(), (
        f"Mock LLM response did not contain the expected note content ('{TEST_NOTE_CONTENT}'). Response: {retrieve_final_text_reply}"
    )
    # Use lower() for case-insensitive comparison
    assert test_note_title.lower() in retrieve_final_text_reply.lower(), (
        f"Mock LLM response did not contain the expected note title ('{test_note_title}'). Response: {retrieve_final_text_reply}"
    )
    assert (
        "Rule-based mock says:" in retrieve_final_text_reply
    )  # Check it used our specific response

    logger.info(
        "Verified rule-based mock response contains note content."  # Tool call check is implicit now
    )
    logger.info("--- Rule-Based Mock Test Passed ---")
