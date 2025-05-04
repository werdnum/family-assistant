import pytest
import uuid
import asyncio
import logging
import json  # Added json import
from sqlalchemy import text  # To query DB directly for assertion
from typing import List, Dict, Any, Optional, Callable, Tuple  # Added typing imports
from unittest.mock import MagicMock  # For mocking Application

# _generate_llm_response_for_chat was moved to ProcessingService
# from family_assistant.main import _generate_llm_response_for_chat

# Import DatabaseContext and getter
from family_assistant.storage.context import DatabaseContext, get_db_context

# Import necessary classes for instantiation
from family_assistant.processing import ProcessingService
from family_assistant.llm import LLMInterface, LLMOutput  # Keep Interface and Output

# Import the rule-based mock
from tests.mocks.mock_llm import (
    RuleBasedMockLLMClient,
    Rule,
    MatcherFunction,
    get_last_message_text,
)

from family_assistant.tools import (
    LocalToolsProvider,
    MCPToolsProvider,
    CompositeToolsProvider,
    TOOLS_DEFINITION as local_tools_definition,
    AVAILABLE_FUNCTIONS as local_tool_implementations,
)

# Import storage functions for assertion (will use the patched engine)
# from family_assistant.storage.notes import get_note_by_title # Can use this or direct query

logger = logging.getLogger(__name__)

# --- Test Configuration ---
# Use a unique title for each test run to avoid collisions
TEST_NOTE_TITLE_BASE = "Smoketest Note"
TEST_NOTE_CONTENT = "This is the content for the smoke test."
TEST_CHAT_ID = 12345  # Dummy chat ID
TEST_USER_NAME = "TestUser"
# TEST_MODEL_NAME is no longer needed for the mock


@pytest.mark.asyncio
async def test_add_and_retrieve_note_rule_mock(test_db_engine):  # Renamed test
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
    test_note_title = f"{TEST_NOTE_TITLE_BASE} {uuid.uuid4()}"
    test_tool_call_id = f"call_{uuid.uuid4()}"  # Pre-generate ID for the rule
    logger.info(f"\n--- Running Rule-Based Mock Test: Add/Retrieve Note ---")
    logger.info(f"Using Note Title: {test_note_title}")

    # --- Define Rules ---

    # Rule 1: Match Add Note Request
    def add_note_matcher(messages, tools, tool_choice):
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
    def retrieve_note_matcher(messages, tools, tool_choice):
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
    logger.info(f"Using RuleBasedMockLLMClient for testing.")

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

    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        prompts=dummy_prompts,
        calendar_config=dummy_calendar_config,
        timezone_str=dummy_timezone_str,
        max_history_messages=dummy_max_history,
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
        # Unpack all 4 return values, assign unused ones to _
        add_response_content, add_tool_info, _, _ = (
            await processing_service.generate_llm_response_for_chat(
                db_context=db_context,  # Pass the context
                application=mock_application,
                interface_type="test", # Added interface type
                conversation_id=str(TEST_CHAT_ID), # Added conversation ID as string
                trigger_content_parts=add_note_trigger,
                user_name=TEST_USER_NAME,
                # model_name argument removed
            )
        )
        # model_name argument removed from _generate_llm_response_for_chat call

    logger.info(f"Add Note - Mock LLM Response Content: {add_response_content}")
    logger.info(f"Add Note - Tool Info from Processing: {add_tool_info}")

    # Assertion 1: Check the database directly to confirm the note was added
    # Use the test_db_engine yielded by the fixture
    note_in_db = None
    logger.info("Checking database for the new note...")
    async with test_db_engine.connect() as connection:
        result = await connection.execute(
            text("SELECT title, content FROM notes WHERE title = :title"),
            {"title": test_note_title},
        )
        note_in_db = result.fetchone()

    assert (
        note_in_db is not None
    ), f"Note '{test_note_title}' not found in the database after rule-based mock add attempt."
    assert note_in_db.content == TEST_NOTE_CONTENT, "Note content in DB does not match."
    logger.info(f"Verified note '{test_note_title}' exists in DB.")

    # Assertion 2: Check tool info (verifies ProcessingService handled mock output)
    assert add_tool_info is not None, "Tool info should not be None for add_note rule"
    assert len(add_tool_info) == 1, "Expected exactly one tool call info object"
    assert add_tool_info[0]["function_name"] == "add_or_update_note"
    assert (
        add_tool_info[0]["tool_call_id"] == test_tool_call_id
    )  # Check ID matches rule
    assert add_tool_info[0]["arguments"]["title"] == test_note_title
    assert add_tool_info[0]["arguments"]["content"] == TEST_NOTE_CONTENT
    assert "Error:" not in add_tool_info[0].get(
        "response_content", ""
    ), f"Tool execution reported an error: {add_tool_info[0].get('response_content')}"
    logger.info("Tool info check passed.")

    # --- Add a small delay ---
    await asyncio.sleep(0.1)  # Can be shorter with mock

    logger.info(f"\n--- Running Rule-Based Mock Test: Retrieve Note ---")
    # --- Part 2: Retrieve the note ---
    retrieve_note_text = f"What do you know about the note titled '{test_note_title}'?"
    retrieve_note_trigger = [{"type": "text", "text": retrieve_note_text}]

    # Create a new context for the retrieval part (or reuse if appropriate, but new is safer for isolation)
    async with DatabaseContext(engine=test_db_engine) as db_context:
        # Call the method on the ProcessingService instance again
        # Unpack all 4 return values, assign unused ones to _
        retrieve_response_content, retrieve_tool_info, _, _ = (
            await processing_service.generate_llm_response_for_chat(
                db_context=db_context,  # Pass the context
                application=mock_application,
                trigger_content_parts=retrieve_note_trigger,
                user_name=TEST_USER_NAME,
                # model_name argument removed
            )
        )
        # model_name argument removed from _generate_llm_response_for_chat call

    logger.info(
        f"Retrieve Note - Mock LLM Response Content: {retrieve_response_content}"
    )
    logger.info(f"Retrieve Note - Tool Info from Processing: {retrieve_tool_info}")

    # Assertion 3: Check the final response content from the mock rule
    assert (
        retrieve_response_content is not None
    ), "Mock LLM response for retrieval was None."
    # Use lower() for case-insensitive comparison
    assert (
        TEST_NOTE_CONTENT.lower() in retrieve_response_content.lower()
    ), f"Mock LLM response did not contain the expected note content ('{TEST_NOTE_CONTENT}'). Response: {retrieve_response_content}"
    assert (
        test_note_title.lower() in retrieve_response_content.lower()
    ), f"Mock LLM response did not contain the expected note title ('{test_note_title}'). Response: {retrieve_response_content}"
    assert (
        "Rule-based mock says:" in retrieve_response_content
    )  # Check it used our specific response

    # Assertion 4: Ensure no tool was called for retrieval
    assert (
        retrieve_tool_info is None
    ), f"Expected no tool call for mock retrieval rule, but got: {retrieve_tool_info}"

    logger.info(
        "Verified rule-based mock response contains note content and no tool was called."
    )
    logger.info("--- Rule-Based Mock Test Passed ---")
