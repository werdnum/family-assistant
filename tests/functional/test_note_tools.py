"""End-to-end functional tests for note tools."""

import json
import logging
import uuid
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.context_providers import NotesContextProvider
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
from tests.mocks.mock_llm import (
    LLMOutput as MockLLMOutput,
)
from tests.mocks.mock_llm import (
    MatcherArgs,
    Rule,
    RuleBasedMockLLMClient,
    get_last_message_text,
)

if TYPE_CHECKING:
    from family_assistant.llm import LLMInterface

logger = logging.getLogger(__name__)


# Test configuration
TEST_CHAT_ID = 12345
TEST_USER_NAME = "NotesTestUser"


async def create_processing_service(
    test_db_engine: AsyncEngine, rules: list[Rule]
) -> ProcessingService:
    """Helper to create a processing service with given LLM rules."""
    # Create mock LLM
    llm_client: LLMInterface = RuleBasedMockLLMClient(rules=rules)

    # Create tool providers
    local_provider = LocalToolsProvider(
        definitions=local_tools_definition, implementations=local_tool_implementations
    )
    mcp_provider = MCPToolsProvider(mcp_server_configs={})
    composite_provider = CompositeToolsProvider(
        providers=[local_provider, mcp_provider]
    )
    await composite_provider.get_tool_definitions()

    # Create context providers
    async def get_test_db_context_func() -> DatabaseContext:
        manager = get_db_context(engine=test_db_engine)
        return await manager.__aenter__()

    notes_provider = NotesContextProvider(
        get_db_context_func=get_test_db_context_func,
        prompts={"system_prompt": "Test system prompt."},
    )

    # Create service config
    service_config = ProcessingServiceConfig(
        prompts={"system_prompt": "Test system prompt."},
        timezone_str="UTC",
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={},
        delegation_security_level="confirm",
        id="test_notes_profile",
    )

    return ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        context_providers=[notes_provider],
        service_config=service_config,
        server_url=None,
        app_config={},
    )


@pytest.mark.asyncio
async def test_add_note_with_include_in_prompt(test_db_engine: AsyncEngine) -> None:
    """Test adding a note with include_in_prompt parameter."""
    # Arrange
    note_title = f"Test Note {uuid.uuid4()}"
    note_content = "This is test content for the note."
    tool_call_id = f"call_{uuid.uuid4()}"

    def add_note_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        tools = kwargs.get("tools")
        last_text = get_last_message_text(messages).lower()
        return (
            "remember" in last_text
            and note_title.lower() in last_text
            and "include in prompt" in last_text
            and tools is not None
        )

    add_note_response = MockLLMOutput(
        content="I'll add that note and include it in the system prompt.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="add_or_update_note",
                    arguments=json.dumps({
                        "title": note_title,
                        "content": note_content,
                        "include_in_prompt": True,
                    }),
                ),
            )
        ],
    )

    processing_service = await create_processing_service(
        test_db_engine, [(add_note_matcher, add_note_response)]
    )

    # Act
    async with DatabaseContext(engine=test_db_engine) as db_context:
        (
            final_text_reply,
            _,
            _,
            error,
        ) = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test",
            conversation_id=str(TEST_CHAT_ID),
            trigger_content_parts=[
                {
                    "type": "text",
                    "text": f"Remember this: {note_title}. Content: {note_content}. Include in prompt.",
                }
            ],
            trigger_interface_message_id="msg_001",
            user_name=TEST_USER_NAME,
        )

    # Assert
    assert error is None
    assert final_text_reply is not None

    # Verify note in database
    async with test_db_engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT title, content, include_in_prompt FROM notes WHERE title = :title"
            ),
            {"title": note_title},
        )
        note_in_db = result.fetchone()

    assert note_in_db is not None
    assert note_in_db.content == note_content
    assert note_in_db.include_in_prompt == 1  # SQLite returns 1 for True


@pytest.mark.asyncio
async def test_get_note_that_exists(test_db_engine: AsyncEngine) -> None:
    """Test retrieving a note that exists."""
    # Arrange
    note_title = f"Existing Note {uuid.uuid4()}"
    note_content = "Content of the existing note."
    tool_call_id = f"call_{uuid.uuid4()}"

    # First add the note to the database
    async with test_db_engine.connect() as connection:
        await connection.execute(
            text(
                "INSERT INTO notes (title, content, include_in_prompt) VALUES (:title, :content, :include)"
            ),
            {"title": note_title, "content": note_content, "include": True},
        )
        await connection.commit()

    def get_note_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        tools = kwargs.get("tools")
        last_text = get_last_message_text(messages).lower()
        return (
            ("get" in last_text or "retrieve" in last_text or "show" in last_text)
            and note_title.lower() in last_text
            and tools is not None
        )

    get_note_response = MockLLMOutput(
        content="Let me retrieve that note for you.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="get_note",
                    arguments=json.dumps({"title": note_title}),
                ),
            )
        ],
    )

    processing_service = await create_processing_service(
        test_db_engine, [(get_note_matcher, get_note_response)]
    )

    # Act
    async with DatabaseContext(engine=test_db_engine) as db_context:
        (
            final_text_reply,
            _,
            _,
            error,
        ) = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test",
            conversation_id=str(TEST_CHAT_ID),
            trigger_content_parts=[
                {"type": "text", "text": f"Get the note titled '{note_title}'"}
            ],
            trigger_interface_message_id="msg_002",
            user_name=TEST_USER_NAME,
        )

    # Assert
    assert error is None
    assert final_text_reply is not None


@pytest.mark.asyncio
async def test_get_note_that_does_not_exist(test_db_engine: AsyncEngine) -> None:
    """Test retrieving a note that doesn't exist."""
    # Arrange
    note_title = f"Nonexistent Note {uuid.uuid4()}"
    tool_call_id = f"call_{uuid.uuid4()}"

    def get_note_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        tools = kwargs.get("tools")
        last_text = get_last_message_text(messages).lower()
        return (
            ("get" in last_text or "retrieve" in last_text)
            and note_title.lower() in last_text
            and tools is not None
        )

    get_note_response = MockLLMOutput(
        content="Let me check for that note.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="get_note",
                    arguments=json.dumps({"title": note_title}),
                ),
            )
        ],
    )

    processing_service = await create_processing_service(
        test_db_engine, [(get_note_matcher, get_note_response)]
    )

    # Act
    async with DatabaseContext(engine=test_db_engine) as db_context:
        (
            final_text_reply,
            _,
            _,
            error,
        ) = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test",
            conversation_id=str(TEST_CHAT_ID),
            trigger_content_parts=[
                {"type": "text", "text": f"Get the note titled '{note_title}'"}
            ],
            trigger_interface_message_id="msg_003",
            user_name=TEST_USER_NAME,
        )

    # Assert
    assert error is None
    assert final_text_reply is not None


@pytest.mark.asyncio
async def test_list_all_notes(test_db_engine: AsyncEngine) -> None:
    """Test listing all notes."""
    # Arrange
    base_title = f"List Test {uuid.uuid4()}"
    notes_data = [
        (f"{base_title} 1", "Content 1", True),
        (f"{base_title} 2", "Content 2", False),
        (f"{base_title} 3", "Content 3", True),
    ]

    # Add test notes
    async with test_db_engine.connect() as connection:
        for title, content, include in notes_data:
            await connection.execute(
                text(
                    "INSERT INTO notes (title, content, include_in_prompt) VALUES (:title, :content, :include)"
                ),
                {"title": title, "content": content, "include": include},
            )
        await connection.commit()

    tool_call_id = f"call_{uuid.uuid4()}"

    def list_notes_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        tools = kwargs.get("tools")
        last_text = get_last_message_text(messages).lower()
        return "list" in last_text and "notes" in last_text and tools is not None

    list_notes_response = MockLLMOutput(
        content="Let me list all the notes.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="list_notes",
                    arguments=json.dumps({}),
                ),
            )
        ],
    )

    processing_service = await create_processing_service(
        test_db_engine, [(list_notes_matcher, list_notes_response)]
    )

    # Act
    async with DatabaseContext(engine=test_db_engine) as db_context:
        (
            final_text_reply,
            _,
            _,
            error,
        ) = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test",
            conversation_id=str(TEST_CHAT_ID),
            trigger_content_parts=[{"type": "text", "text": "List all notes"}],
            trigger_interface_message_id="msg_004",
            user_name=TEST_USER_NAME,
        )

    # Assert
    assert error is None
    assert final_text_reply is not None


@pytest.mark.asyncio
async def test_list_notes_with_filter(test_db_engine: AsyncEngine) -> None:
    """Test listing notes with include_in_prompt filter."""
    # Arrange
    base_title = f"Filter Test {uuid.uuid4()}"
    notes_data = [
        (f"{base_title} Included", "Content 1", True),
        (f"{base_title} Excluded", "Content 2", False),
    ]

    # Add test notes
    async with test_db_engine.connect() as connection:
        for title, content, include in notes_data:
            await connection.execute(
                text(
                    "INSERT INTO notes (title, content, include_in_prompt) VALUES (:title, :content, :include)"
                ),
                {"title": title, "content": content, "include": include},
            )
        await connection.commit()

    tool_call_id = f"call_{uuid.uuid4()}"

    def list_included_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        tools = kwargs.get("tools")
        last_text = get_last_message_text(messages).lower()
        return (
            "list" in last_text
            and "included in prompt" in last_text
            and tools is not None
        )

    list_included_response = MockLLMOutput(
        content="Let me list notes included in the prompt.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="list_notes",
                    arguments=json.dumps({"include_in_prompt": True}),
                ),
            )
        ],
    )

    processing_service = await create_processing_service(
        test_db_engine, [(list_included_matcher, list_included_response)]
    )

    # Act
    async with DatabaseContext(engine=test_db_engine) as db_context:
        (
            final_text_reply,
            _,
            _,
            error,
        ) = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test",
            conversation_id=str(TEST_CHAT_ID),
            trigger_content_parts=[
                {"type": "text", "text": "List notes that are included in prompt"}
            ],
            trigger_interface_message_id="msg_005",
            user_name=TEST_USER_NAME,
        )

    # Assert
    assert error is None
    assert final_text_reply is not None


@pytest.mark.asyncio
async def test_delete_note(test_db_engine: AsyncEngine) -> None:
    """Test deleting a note."""
    # Arrange
    note_title = f"Delete Me {uuid.uuid4()}"
    note_content = "This note will be deleted."

    # First add the note
    async with test_db_engine.connect() as connection:
        await connection.execute(
            text(
                "INSERT INTO notes (title, content, include_in_prompt) VALUES (:title, :content, :include)"
            ),
            {"title": note_title, "content": note_content, "include": True},
        )
        await connection.commit()

    tool_call_id = f"call_{uuid.uuid4()}"

    def delete_note_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        tools = kwargs.get("tools")
        last_text = get_last_message_text(messages).lower()
        return (
            "delete" in last_text
            and note_title.lower() in last_text
            and tools is not None
        )

    delete_note_response = MockLLMOutput(
        content="I'll delete that note for you.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="delete_note",
                    arguments=json.dumps({"title": note_title}),
                ),
            )
        ],
    )

    processing_service = await create_processing_service(
        test_db_engine, [(delete_note_matcher, delete_note_response)]
    )

    # Act
    async with DatabaseContext(engine=test_db_engine) as db_context:
        (
            final_text_reply,
            _,
            _,
            error,
        ) = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test",
            conversation_id=str(TEST_CHAT_ID),
            trigger_content_parts=[
                {"type": "text", "text": f"Delete the note titled '{note_title}'"}
            ],
            trigger_interface_message_id="msg_006",
            user_name=TEST_USER_NAME,
        )

    # Assert
    assert error is None
    assert final_text_reply is not None

    # Verify note was deleted
    async with test_db_engine.connect() as connection:
        result = await connection.execute(
            text("SELECT COUNT(*) as count FROM notes WHERE title = :title"),
            {"title": note_title},
        )
        count = result.scalar()

    assert count == 0


@pytest.mark.asyncio
async def test_update_existing_note(test_db_engine: AsyncEngine) -> None:
    """Test updating an existing note's content."""
    # Arrange
    note_title = f"Update Me {uuid.uuid4()}"
    original_content = "Original content."
    updated_content = "Updated content with new information."

    # First add the note
    async with test_db_engine.connect() as connection:
        await connection.execute(
            text(
                "INSERT INTO notes (title, content, include_in_prompt) VALUES (:title, :content, :include)"
            ),
            {"title": note_title, "content": original_content, "include": True},
        )
        await connection.commit()

    tool_call_id = f"call_{uuid.uuid4()}"

    def update_note_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        tools = kwargs.get("tools")
        last_text = get_last_message_text(messages).lower()
        return (
            "update" in last_text
            and note_title.lower() in last_text
            and updated_content.lower() in last_text
            and tools is not None
        )

    update_note_response = MockLLMOutput(
        content="I'll update that note with the new content.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="add_or_update_note",
                    arguments=json.dumps({
                        "title": note_title,
                        "content": updated_content,
                        "include_in_prompt": True,
                    }),
                ),
            )
        ],
    )

    processing_service = await create_processing_service(
        test_db_engine, [(update_note_matcher, update_note_response)]
    )

    # Act
    async with DatabaseContext(engine=test_db_engine) as db_context:
        (
            final_text_reply,
            _,
            _,
            error,
        ) = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test",
            conversation_id=str(TEST_CHAT_ID),
            trigger_content_parts=[
                {
                    "type": "text",
                    "text": f"Update the note '{note_title}' with this content: {updated_content}",
                }
            ],
            trigger_interface_message_id="msg_007",
            user_name=TEST_USER_NAME,
        )

    # Assert
    assert error is None
    assert final_text_reply is not None

    # Verify note was updated
    async with test_db_engine.connect() as connection:
        result = await connection.execute(
            text("SELECT content FROM notes WHERE title = :title"),
            {"title": note_title},
        )
        note_in_db = result.fetchone()

    assert note_in_db is not None
    assert note_in_db.content == updated_content
