import asyncio
import contextlib  # Added contextlib import
import json  # Added json import
import logging

# Import storage functions for assertion (will use the patched engine)
import uuid  # Added for turn_id
from unittest.mock import MagicMock  # For mocking Application

import pytest
from sqlalchemy import text  # To query DB directly for assertion
from sqlalchemy.ext.asyncio import AsyncEngine  # Added for type hints

# Import ContextProvider and NotesContextProvider
from family_assistant.context_providers import (
    NotesContextProvider,
)
from family_assistant.llm import LLMInterface, LLMOutput  # Keep Interface and Output

# Import necessary classes for instantiation
from family_assistant.processing import ProcessingService

# _generate_llm_response_for_chat was moved to ProcessingService
# Import DatabaseContext and getter
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
        # tool_choice = kwargs.get("tool_choice") # Not used by this matcher

        last_text = get_last_message_text(messages).lower()
        return (
            "remember this note" in last_text
            and f"title: {test_note_title}".lower() in last_text
            and f"content: {TEST_NOTE_CONTENT}".lower() in last_text
            and tools is not None  # Check that tools were actually provided
        )

    add_note_response = LLMOutput(
        content="OK, I will add that note via the rule-based mock.",
        tool_calls=[
            {
                "id": test_tool_call_id,  # Use pre-generated ID
                "type": "function",
                "function": {
                    "name": "add_or_update_note",
                    "arguments": json.dumps(
                        {  # Arguments must be JSON string
                            "title": test_note_title,
                            "content": TEST_NOTE_CONTENT,
                        }
                    ),
                },
            }
        ],
    )
    add_note_rule: Rule = (add_note_matcher, add_note_response)

    # Rule 2: Match Retrieve Note Request
    def retrieve_note_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        # tools = kwargs.get("tools") # Not used by this matcher
        # tool_choice = kwargs.get("tool_choice") # Not used by this matcher

        last_text = get_last_message_text(messages).lower()
        # NOTE: This simple rule-based mock is stateless.
        # It doesn't know if the note was *actually* added before.
        # We rely on the test structure to call add before retrieve.
        return (
            "what do you know about" in last_text
            and f"note titled '{test_note_title}'".lower() in last_text
        )

    retrieve_note_response = LLMOutput(
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

    # --- Instantiate Context Providers ---
    # Function to get DB context for the specific test engine
    async def get_test_db_context_func() -> (
        contextlib.AbstractAsyncContextManager[DatabaseContext]
    ):
        return await get_db_context(engine=test_db_engine)

    notes_provider = NotesContextProvider(
        get_db_context_func=get_test_db_context_func,
        prompts=dummy_prompts,
    )

    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        prompts=dummy_prompts,
        calendar_config=dummy_calendar_config,
        timezone_str=dummy_timezone_str,
        max_history_messages=dummy_max_history,
        context_providers=[notes_provider],  # Pass the notes provider
        history_max_age_hours=dummy_history_age,
        server_url=None,  # Added missing argument
    )
    logger.info(
        f"Instantiated ProcessingService with {type(llm_client).__name__}, {type(composite_provider).__name__} and dummy config"
    )

    # Mock Application instance needed for ToolExecutionContext
    mock_application = MagicMock()
    # If specific attributes/methods of application are needed by tools, mock them here
    # e.g., mock_application.bot.send_message = AsyncMock()

    # --- Part 1: Add the note ---
    add_note_text = f"Please remember this note. Title: {test_note_title}. Content: {TEST_NOTE_CONTENT}"
    add_note_trigger = [{"type": "text", "text": add_note_text}]

    # Create a DatabaseContext using the test engine provided by the fixture
    # Note: test_db_engine fixture comes from the root conftest.py
    async with DatabaseContext(engine=test_db_engine) as db_context:
        # Call the method on the ProcessingService instance
        # Unpack the 3 return values correctly
        (
            add_turn_messages,
            add_reasoning_info,
            add_error,
        ) = await processing_service.generate_llm_response_for_chat(
            db_context=db_context,  # Pass the context
            application=mock_application,
            interface_type="test",  # Added interface type
            conversation_id=str(TEST_CHAT_ID),  # Added conversation ID as string
            turn_id=str(uuid.uuid4()),  # Added turn_id
            trigger_content_parts=add_note_trigger,
            trigger_interface_message_id=str(
                user_message_id_add
            ),  # Added missing argument
            user_name=TEST_USER_NAME,
            # model_name argument removed
        )
    # Assertions remain outside the context manager
    assert (
        add_error is None
    ), f"Error during add note: {add_error}"  # Use correct error variable
    assert add_turn_messages, "No messages generated during add note turn"

    # Assertion 1: Check the database directly to confirm the note was added
    # Use the test_db_engine yielded by the fixture
    note_in_db = None
    # Find the assistant message requesting the tool call
    assistant_add_request = next(
        (
            msg
            for msg in add_turn_messages
            if msg.get("role") == "assistant" and msg.get("tool_calls")
        ),
        None,
    )
    assert assistant_add_request is not None, "Assistant did not request tool call"
    assert assistant_add_request["tool_calls"], "Tool calls list is empty"
    assert assistant_add_request["tool_calls"][0]["id"] == test_tool_call_id
    assert (
        assistant_add_request["tool_calls"][0]["function"]["name"]
        == "add_or_update_note"
    )

    note_in_db = None  # Correct indentation

    logger.info("Checking database for the new note...")  # Correct indentation
    async with test_db_engine.connect() as connection:  # Correct indentation
        result = await connection.execute(
            text("SELECT title, content FROM notes WHERE title = :title"),
            {"title": test_note_title},
        )
        note_in_db = result.fetchone()

    assert (
        note_in_db is not None
    ), f"Note '{test_note_title}' not found in the database after rule-based mock add attempt."
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
            retrieve_turn_messages,
            _,
            retrieve_error,
        ) = await processing_service.generate_llm_response_for_chat(
            db_context=db_context,  # Pass the context
            interface_type="test",  # Added missing interface type
            conversation_id=str(TEST_CHAT_ID),  # Added missing conversation ID
            application=mock_application,
            turn_id=str(uuid.uuid4()),  # Added turn_id
            trigger_content_parts=retrieve_note_trigger,
            trigger_interface_message_id=str(
                user_message_id_retrieve
            ),  # Added missing argument
            user_name=TEST_USER_NAME,
            # model_name argument removed
        )

        # model_name argument removed
    assert add_error is None, f"Error during add note: {add_error}"
    assert add_turn_messages, "No messages generated during add note turn"
    assert add_error is None, f"Error during add note: {add_error}"
    assert add_turn_messages, "No messages generated during add note turn"
    # Find the final assistant message
    final_assistant_message = next(
        (
            msg
            for msg in reversed(retrieve_turn_messages)
            if msg.get("role") == "assistant"
        ),
        None,
    )
    assert final_assistant_message is not None, "No final assistant message found"
    assert (
        final_assistant_message.get("tool_calls") is None
    ), "LLM made an unexpected tool call for retrieval"
    assert final_assistant_message.get("content") is not None

    # Assertion 3: Check the final response content from the mock rule
    # Use lower() for case-insensitive comparison # Marked line 244
    assert (
        TEST_NOTE_CONTENT.lower() in final_assistant_message["content"].lower()
    ), f"Mock LLM response did not contain the expected note content ('{TEST_NOTE_CONTENT}'). Response: {final_assistant_message['content']}"
    # Use lower() for case-insensitive comparison
    assert (
        test_note_title.lower() in final_assistant_message["content"].lower()
    ), f"Mock LLM response did not contain the expected note title ('{test_note_title}'). Response: {final_assistant_message['content']}"
    assert (
        "Rule-based mock says:" in final_assistant_message["content"]
    )  # Check it used our specific response

    logger.info(
        "Verified rule-based mock response contains note content and no tool was called."
    )
    logger.info("--- Rule-Based Mock Test Passed ---")
