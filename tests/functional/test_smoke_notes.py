import pytest
import uuid
import asyncio
import logging
import os # Import os to read environment variables
from sqlalchemy import text # To query DB directly for assertion

# Import the function we want to test directly
from family_assistant.main import _generate_llm_response_for_chat
# Import storage functions for assertion (will use the patched engine)
# from family_assistant.storage.notes import get_note_by_title # Can use this or direct query

logger = logging.getLogger(__name__)

# --- Test Configuration ---
# Use a unique title for each test run to avoid collisions
TEST_NOTE_TITLE_BASE = "Smoketest Note"
TEST_NOTE_CONTENT = "This is the content for the smoke test."
TEST_CHAT_ID = 12345  # Dummy chat ID
TEST_USER_NAME = "TestUser"
# Get model name from env var or use the same default as in main.py's arg parser
TEST_MODEL_NAME = os.getenv("LLM_MODEL", "openrouter/google/gemini-flash-1.5")

@pytest.mark.asyncio
async def test_add_and_retrieve_note_smoke(test_db_engine): # Request the fixture
    """
    Smoke test:
    1. Simulate adding a note via LLM interaction (using real LLM).
    2. Verify the note exists in the (in-memory) database.
    3. Simulate asking about the note.
    4. Verify the LLM's response includes the note content.

    Requires OPENROUTER_API_KEY and TELEGRAM_BOT_TOKEN (can be dummy) env vars.
    """
    # Generate a unique title for this specific test run
    test_note_title = f"{TEST_NOTE_TITLE_BASE} {uuid.uuid4()}"
    logger.info(f"\n--- Running Smoke Test: Add Note ---")
    logger.info(f"Using Note Title: {test_note_title}")

    # --- Part 1: Add the note ---
    add_note_text = f"Please remember this note. Title: {test_note_title}. Content: {TEST_NOTE_CONTENT}"
    add_note_trigger = [{"type": "text", "text": add_note_text}]

    # Call the core logic function directly
    # This will trigger the real LLM, tool detection, and storage call (using patched DB engine)
    add_response_content, add_tool_info = await _generate_llm_response_for_chat(
        chat_id=TEST_CHAT_ID,
        trigger_content_parts=add_note_trigger,
        user_name=TEST_USER_NAME,
        model_name=TEST_MODEL_NAME, # Pass the model name
    )

    logger.info(f"Add Note - LLM Response: {add_response_content}")
    logger.info(f"Add Note - Tool Info: {add_tool_info}")

    # Assertion 1: Check the database directly to confirm the note was added
    # Use the test_db_engine yielded by the fixture
    note_in_db = None
    logger.info("Checking database for the new note...")
    async with test_db_engine.connect() as connection:
         result = await connection.execute(
             text("SELECT title, content FROM notes WHERE title = :title"),
             {"title": test_note_title}
         )
         note_in_db = result.fetchone()

    assert note_in_db is not None, f"Note '{test_note_title}' not found in the database after add attempt."
    assert note_in_db.content == TEST_NOTE_CONTENT, "Note content in DB does not match."
    logger.info(f"Verified note '{test_note_title}' exists in DB.")

    # Assertion 2: Check tool info indicates success
    assert add_tool_info is not None, "Tool info should not be None for add_note"
    # Sometimes the LLM might respond directly without confirming the tool use if it's simple enough.
    # Let's make this assertion more flexible or remove it for the smoke test if it proves flaky.
    # For now, we'll just check that *if* tool info is present, it looks correct.
    if add_tool_info:
        assert len(add_tool_info) == 1, "Expected one tool call if tool info is present"
        assert add_tool_info[0]['function_name'] == 'add_or_update_note'
        # Check response content *within* the tool info dict
        assert "Error:" not in add_tool_info[0].get('response_content', ''), \
            f"Tool response content indicates an error: {add_tool_info[0].get('response_content')}"
        logger.info("Tool info check passed (or no tool info returned, which is acceptable).")
    else:
        logger.info("No tool info returned by LLM for add_note (might be acceptable).")


    # --- Add a small delay before the next step (optional, mimics user pause) ---
    await asyncio.sleep(1)

    logger.info(f"\n--- Running Smoke Test: Retrieve Note ---")
    # --- Part 2: Retrieve the note ---
    retrieve_note_text = f"What do you know about the note titled '{test_note_title}'?"
    retrieve_note_trigger = [{"type": "text", "text": retrieve_note_text}]

    # Call the core logic again
    retrieve_response_content, retrieve_tool_info = await _generate_llm_response_for_chat(
        chat_id=TEST_CHAT_ID,
        trigger_content_parts=retrieve_note_trigger,
        user_name=TEST_USER_NAME,
        model_name=TEST_MODEL_NAME, # Pass the model name
    )

    logger.info(f"Retrieve Note - LLM Response: {retrieve_response_content}")
    logger.info(f"Retrieve Note - Tool Info: {retrieve_tool_info}")

    # Assertion 3: Check the final LLM response contains the note content
    assert retrieve_response_content is not None, "LLM response for retrieval was None."
    # Use lower() for case-insensitive comparison
    assert TEST_NOTE_CONTENT.lower() in retrieve_response_content.lower(), \
        f"LLM response did not contain the expected note content ('{TEST_NOTE_CONTENT}'). Response: {retrieve_response_content}"
    # Assertion 4: Ensure no tool was called for retrieval (LLM should use context)
    assert retrieve_tool_info is None, f"Expected no tool call for simple retrieval, but got: {retrieve_tool_info}"

    logger.info("Verified LLM response contains note content and no tool was called.")
    logger.info("--- Smoke Test Passed ---")
