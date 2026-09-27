"""End-to-end functional tests for note tools."""

import json
import logging
import uuid
from typing import TYPE_CHECKING
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.context_providers import NotesContextProvider
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.security.note_provenance import NoteProvenanceStamp
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.notes import (
    NoteModel,
    NoteReadPolicy,
    NoteWritePolicy,
)
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
    extract_text_from_content,
    get_last_message_text,
    get_message_content,
    get_message_role,
    last_real_message,
)

if TYPE_CHECKING:
    from family_assistant.llm import LLMInterface

logger = logging.getLogger(__name__)


# Test configuration
TEST_CHAT_ID = 12345
TEST_USER_NAME = "NotesTestUser"


async def seed_note(
    db: Database, title: str, content: str, *, include_in_prompt: bool
) -> None:
    await db.notes.add_or_update(
        title=title,
        content=content,
        include_in_prompt=include_in_prompt,
        write_policy=NoteWritePolicy.UNCONSTRAINED,
        provenance=NoteProvenanceStamp.internal(),
    )


async def read_note(db: Database, title: str) -> NoteModel | None:
    return await db.notes.get_by_title(title, read_policy=NoteReadPolicy.UNRESTRICTED)


def tool_result_capture() -> tuple[Rule, list[str]]:
    """A rule that records each tool result the LLM is shown, and ends the turn."""
    captured: list[str] = []

    def newest_is_tool_result(kwargs: MatcherArgs) -> bool:
        message = last_real_message(kwargs.get("messages", []))
        return message is not None and get_message_role(message) == "tool"

    def record(kwargs: MatcherArgs) -> MockLLMOutput:
        message = last_real_message(kwargs["messages"])
        assert message is not None
        captured.append(extract_text_from_content(get_message_content(message)))
        return MockLLMOutput(content="Done.")

    return (newest_is_tool_result, record), captured


async def create_processing_service(
    db_engine: AsyncEngine, rules: list[Rule]
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
    def get_test_db_context_func() -> Database:
        return Database(engine=db_engine)

    notes_provider = NotesContextProvider(
        get_db_context_func=get_test_db_context_func,
        prompts={"system_prompt": "Test system prompt."},
        read_policy=NoteReadPolicy.UNRESTRICTED,
    )

    # Create service config
    service_config = ProcessingServiceConfig(
        prompts={"system_prompt": "Test system prompt."},
        timezone=ZoneInfo("UTC"),
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.CONFIRM,
        id="test_notes_profile",
    )

    return ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        context_providers=[notes_provider],
        service_config=service_config,
        server_url=None,
        app_config=AppConfig(),
    )


@pytest.mark.asyncio
async def test_add_note_with_include_in_prompt(db_engine: AsyncEngine) -> None:
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
        db_engine, [(add_note_matcher, add_note_response)]
    )

    # Act
    db_context = Database(engine=db_engine)
    result = await processing_service.handle_chat_interaction(
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
    assert result.error_traceback is None
    assert result.text_reply is not None

    note_in_db = await read_note(db_context, note_title)
    assert note_in_db is not None
    assert note_in_db.content == note_content
    assert note_in_db.include_in_prompt is True


@pytest.mark.asyncio
async def test_get_note_that_exists(db_engine: AsyncEngine) -> None:
    """Test retrieving a note that exists."""
    # Arrange
    note_title = f"Existing Note {uuid.uuid4()}"
    note_content = "Content of the existing note."
    tool_call_id = f"call_{uuid.uuid4()}"
    db_context = Database(engine=db_engine)
    await seed_note(db_context, note_title, note_content, include_in_prompt=True)
    capture_rule, tool_results = tool_result_capture()

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
        db_engine, [capture_rule, (get_note_matcher, get_note_response)]
    )

    # Act
    result = await processing_service.handle_chat_interaction(
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
    assert result.error_traceback is None
    assert len(tool_results) == 1
    note_payload = json.loads(tool_results[0])
    assert note_payload["exists"] is True
    assert note_payload["title"] == note_title
    assert note_payload["content"] == note_content
    assert note_payload["include_in_prompt"] is True


@pytest.mark.asyncio
async def test_get_note_that_does_not_exist(db_engine: AsyncEngine) -> None:
    """Test retrieving a note that doesn't exist."""
    # Arrange
    note_title = f"Nonexistent Note {uuid.uuid4()}"
    tool_call_id = f"call_{uuid.uuid4()}"
    capture_rule, tool_results = tool_result_capture()

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
        db_engine, [capture_rule, (get_note_matcher, get_note_response)]
    )

    # Act
    db_context = Database(engine=db_engine)
    result = await processing_service.handle_chat_interaction(
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
    assert result.error_traceback is None
    assert len(tool_results) == 1
    note_payload = json.loads(tool_results[0])
    assert note_payload["exists"] is False
    assert note_payload["title"] == note_title
    assert note_payload["content"] is None


@pytest.mark.asyncio
async def test_list_all_notes(db_engine: AsyncEngine) -> None:
    """Test listing all notes."""
    # Arrange
    base_title = f"List Test {uuid.uuid4()}"
    notes_data = [
        (f"{base_title} 1", "Content 1", True),
        (f"{base_title} 2", "Content 2", False),
        (f"{base_title} 3", "Content 3", True),
    ]
    db_context = Database(engine=db_engine)
    for title, content, include in notes_data:
        await seed_note(db_context, title, content, include_in_prompt=include)
    capture_rule, tool_results = tool_result_capture()

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
        db_engine, [capture_rule, (list_notes_matcher, list_notes_response)]
    )

    # Act
    result = await processing_service.handle_chat_interaction(
        db_context=db_context,
        chat_interface=MagicMock(),
        interface_type="test",
        conversation_id=str(TEST_CHAT_ID),
        trigger_content_parts=[{"type": "text", "text": "List all notes"}],
        trigger_interface_message_id="msg_004",
        user_name=TEST_USER_NAME,
    )

    # Assert
    assert result.error_traceback is None
    assert len(tool_results) == 1
    listed = {
        entry["title"]: (entry["content_preview"], entry["include_in_prompt"])
        for entry in json.loads(tool_results[0])
    }
    for title, content, include in notes_data:
        assert listed.get(title) == (content, include)


@pytest.mark.asyncio
async def test_list_notes_with_filter(db_engine: AsyncEngine) -> None:
    """Test listing notes with include_in_prompt filter."""
    # Arrange
    base_title = f"Filter Test {uuid.uuid4()}"
    included_title = f"{base_title} Included"
    excluded_title = f"{base_title} Excluded"
    db_context = Database(engine=db_engine)
    await seed_note(db_context, included_title, "Content 1", include_in_prompt=True)
    await seed_note(db_context, excluded_title, "Content 2", include_in_prompt=False)
    capture_rule, tool_results = tool_result_capture()

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
        db_engine, [capture_rule, (list_included_matcher, list_included_response)]
    )

    # Act
    result = await processing_service.handle_chat_interaction(
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
    assert result.error_traceback is None
    assert len(tool_results) == 1
    listed = json.loads(tool_results[0])
    listed_titles = {entry["title"] for entry in listed}
    assert included_title in listed_titles
    assert excluded_title not in listed_titles
    assert all(entry["include_in_prompt"] is True for entry in listed)


@pytest.mark.asyncio
async def test_delete_note(db_engine: AsyncEngine) -> None:
    """Test deleting a note."""
    # Arrange
    note_title = f"Delete Me {uuid.uuid4()}"
    note_content = "This note will be deleted."
    db_context = Database(engine=db_engine)
    await seed_note(db_context, note_title, note_content, include_in_prompt=True)

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
        db_engine, [(delete_note_matcher, delete_note_response)]
    )

    # Act
    result = await processing_service.handle_chat_interaction(
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
    assert result.error_traceback is None
    assert result.text_reply is not None
    assert await read_note(db_context, note_title) is None


@pytest.mark.asyncio
async def test_update_existing_note(db_engine: AsyncEngine) -> None:
    """Test updating an existing note's content."""
    # Arrange
    note_title = f"Update Me {uuid.uuid4()}"
    original_content = "Original content."
    updated_content = "Updated content with new information."
    db_context = Database(engine=db_engine)
    await seed_note(db_context, note_title, original_content, include_in_prompt=True)

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
        db_engine, [(update_note_matcher, update_note_response)]
    )

    # Act
    result = await processing_service.handle_chat_interaction(
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
    assert result.error_traceback is None
    assert result.text_reply is not None

    note_in_db = await read_note(db_context, note_title)
    assert note_in_db is not None
    assert note_in_db.content == updated_content
