"""Functional tests for message history storage operations."""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm.messages import (
    AssistantMessage,
    SystemMessage,
    ToolMessage,
)
from family_assistant.security.taint import (
    InMemoryTurnTaintTracker,
    SensitiveReadScope,
    SourceTrustTier,
    TaintPolicyConfig,
    TaintPolicyMode,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.storage.database import Database, set_engine_history_taint_epoch
from family_assistant.storage.message_history import (
    add_message_to_history,
    get_message_by_interface_id,
    get_messages_by_thread_id,
    get_messages_by_turn_id,
    get_recent_history,
    update_message_interface_id,
)
from family_assistant.tools import LOCAL_TOOL_REGISTRATIONS
from family_assistant.tools.infrastructure import (
    LocalToolsProvider,
    PolicyEnforcingToolsProvider,
    TaintTrackingToolsProvider,
)
from family_assistant.tools.policy import (
    PolicyEngine,
    ToolPolicyConfig,
    ToolPolicyDecision,
)
from family_assistant.tools.types import ToolExecutionContext, ToolResult


@pytest.fixture
def db_context(db_engine: AsyncEngine) -> Database:
    """Database handle over the backend-parameterized, migrated test engine."""
    return Database(engine=db_engine, base_delay=0.01)


@pytest.mark.asyncio
async def test_add_message_stores_optional_fields(db_context: Database) -> None:
    """Verify storing messages with optional fields populated."""
    # Arrange
    interface_type = "test_optional"  # Define interface_type
    conversation_id = str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    thread_root_id = 123  # Assume this ID exists from a previous message
    now = datetime.now(UTC)
    role = "assistant"
    tool_calls_data = [
        {
            "id": "call_abc",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"location": "London"}'},
        }
    ]
    reasoning_data = {
        "model": "test-model",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    error_trace = "Something went wrong"
    tool_call_id = "call_abc"  # For a potential 'tool' role message

    # Act: Store messages using the yielded, entered context
    # Store an assistant message with tool calls and reasoning
    assistant_msg_result = await add_message_to_history(
        db_context=db_context,  # Use the yielded context directly
        interface_type=interface_type,
        conversation_id=conversation_id,
        interface_message_id=None,  # Assistant msg might not have one initially
        turn_id=turn_id,
        thread_root_id=thread_root_id,
        timestamp=now,
        role=role,
        content="Calling tool...",
        tool_calls=tool_calls_data,
        reasoning_info=reasoning_data,
    )
    # Store a tool response message
    tool_msg_result = await add_message_to_history(  # Renamed variable to avoid confusion
        db_context=db_context,  # Use the yielded context directly
        interface_type=interface_type,
        conversation_id=conversation_id,
        interface_message_id=None,
        turn_id=turn_id,
        thread_root_id=thread_root_id,
        timestamp=now + timedelta(milliseconds=100),
        role="tool",
        content="Weather is sunny",
        tool_call_id=tool_call_id,
        error_traceback=error_trace,  # Can store traceback even for non-error roles if needed
    )

    assert assistant_msg_result is not None
    assistant_msg_internal_id = assistant_msg_result
    assert tool_msg_result is not None
    tool_msg_internal_id = tool_msg_result

    assistant_result = await db_context.message_history.get_row_by_internal_id(
        assistant_msg_internal_id
    )
    assert assistant_result is not None
    assert assistant_result["turn_id"] == turn_id
    assert assistant_result["thread_root_id"] == thread_root_id
    assert assistant_result["tool_calls"] == tool_calls_data
    assert assistant_result["reasoning_info"] == reasoning_data
    assert assistant_result["tool_call_id"] is None
    assert assistant_result["error_traceback"] is None

    tool_result = await db_context.message_history.get_row_by_internal_id(
        tool_msg_internal_id
    )
    assert tool_result is not None
    assert tool_result["turn_id"] == turn_id
    assert tool_result["thread_root_id"] == thread_root_id
    assert tool_result["tool_call_id"] == tool_call_id
    assert tool_result["error_traceback"] == error_trace
    assert tool_result["tool_calls"] is None
    assert tool_result["reasoning_info"] is None


@pytest.mark.asyncio
async def test_get_recent_history_returns_newest_messages_of_conversation_oldest_first(
    db_context: Database,
) -> None:
    """get_recent_history keeps the newest `limit` rows of one conversation, in order."""
    interface = "history_test"
    conv_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    msg1_id_result = await add_message_to_history(
        db_context,
        interface_type=interface,
        conversation_id=conv_id,
        interface_message_id="msg1",
        turn_id=None,
        thread_root_id=None,
        timestamp=now - timedelta(minutes=10),
        role="user",
        content="Old message",
    )
    msg2_id_result = await add_message_to_history(
        db_context,
        interface_type=interface,
        conversation_id=conv_id,
        interface_message_id="msg2",
        turn_id=None,
        thread_root_id=None,
        timestamp=now - timedelta(minutes=2),
        role="assistant",
        content="Recent 1",
    )
    msg3_id_result = await add_message_to_history(
        db_context,
        interface_type=interface,
        conversation_id=conv_id,
        interface_message_id="msg3",
        turn_id=None,
        thread_root_id=None,
        timestamp=now - timedelta(minutes=1),
        role="user",
        content="Recent 2",
    )
    # Newest of all, so a missing conversation filter would displace msg2.
    await add_message_to_history(
        db_context,
        interface_type=interface,
        conversation_id="other_conv",
        interface_message_id="msg_other",
        turn_id=None,
        thread_root_id=None,
        timestamp=now,
        role="user",
        content="Other convo",
    )

    recent_messages = await get_recent_history(
        db_context,
        interface_type=interface,
        conversation_id=conv_id,
        limit=2,
        max_age=timedelta(hours=1),
    )

    assert msg1_id_result is not None
    assert msg2_id_result is not None
    assert msg3_id_result is not None
    assert [(m["internal_id"], m["content"]) for m in recent_messages] == [
        (msg2_id_result, "Recent 1"),
        (msg3_id_result, "Recent 2"),
    ]


@pytest.mark.asyncio
async def test_get_recent_history_excludes_messages_older_than_max_age(
    db_context: Database,
) -> None:
    interface = "history_age_test"
    conv_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    await add_message_to_history(
        db_context,
        interface_type=interface,
        conversation_id=conv_id,
        interface_message_id="old",
        turn_id=None,
        thread_root_id=None,
        timestamp=now - timedelta(hours=2),
        role="user",
        content="Too old",
    )
    recent_id = await add_message_to_history(
        db_context,
        interface_type=interface,
        conversation_id=conv_id,
        interface_message_id="recent",
        turn_id=None,
        thread_root_id=None,
        timestamp=now - timedelta(minutes=1),
        role="user",
        content="Recent",
    )

    recent_messages = await get_recent_history(
        db_context,
        interface_type=interface,
        conversation_id=conv_id,
        limit=10,
        max_age=timedelta(hours=1),
    )

    assert recent_id is not None
    assert [(m["internal_id"], m["content"]) for m in recent_messages] == [
        (recent_id, "Recent")
    ]


@pytest.mark.asyncio
async def test_get_message_by_interface_id_retrieval(
    db_context: Database,
) -> None:
    """Verify retrieving a specific message by its interface identifiers."""
    # Arrange
    interface = "get_by_id"
    conv_id = str(uuid.uuid4())
    msg_id = "message_abc"
    now = datetime.now(UTC)
    content = "Target message"

    internal_id_result = await add_message_to_history(
        db_context,
        interface_type=interface,
        conversation_id=conv_id,
        interface_message_id=msg_id,
        turn_id=None,
        thread_root_id=None,
        timestamp=now,
        role="user",
        content=content,
    )
    assert internal_id_result is not None

    # Act: Retrieve the message
    retrieved_message = await get_message_by_interface_id(
        db_context, interface, conv_id, msg_id
    )

    assert retrieved_message is not None
    assert retrieved_message["internal_id"] == internal_id_result
    assert retrieved_message["interface_type"] == interface
    assert retrieved_message["conversation_id"] == conv_id
    assert retrieved_message["interface_message_id"] == msg_id
    assert retrieved_message["content"] == content

    # Act: Try to retrieve non-existent message (needs context)
    not_found_message = await get_message_by_interface_id(
        db_context, interface, conv_id, "non_existent_id"
    )

    # Assert
    assert not_found_message is None


@pytest.mark.asyncio
async def test_get_messages_by_turn_id_retrieves_correct_sequence(
    db_context: Database,
) -> None:
    """Verify retrieving all messages for a specific turn_id in order."""
    # Arrange
    interface = "turn_test"
    conv_id = str(uuid.uuid4())
    turn_1 = str(uuid.uuid4())
    turn_2 = str(uuid.uuid4())
    now = datetime.now(UTC)

    # Turn 1 messages
    t1_msg1_result = await add_message_to_history(
        db_context,
        interface_type=interface,
        conversation_id=conv_id,
        interface_message_id=None,
        turn_id=turn_1,
        thread_root_id=1,
        timestamp=now,
        role="assistant",
        content="T1 Call tool",
    )
    t1_msg2_result = await add_message_to_history(
        db_context,
        interface_type=interface,
        conversation_id=conv_id,
        interface_message_id=None,
        turn_id=turn_1,
        thread_root_id=1,
        timestamp=now + timedelta(seconds=1),
        role="tool",
        content="T1 Tool result",
    )
    t1_msg3_result = await add_message_to_history(
        db_context,
        interface_type=interface,
        conversation_id=conv_id,
        interface_message_id=None,
        turn_id=turn_1,
        thread_root_id=1,
        timestamp=now + timedelta(seconds=2),
        role="assistant",
        content="T1 Final answer",
    )
    # Turn 2 message
    await add_message_to_history(
        db_context,
        interface_type=interface,
        conversation_id=conv_id,
        interface_message_id=None,
        turn_id=turn_2,
        thread_root_id=1,
        timestamp=now + timedelta(seconds=3),
        role="assistant",
        content="T2 Different turn",
    )
    # Message with no turn id
    await add_message_to_history(
        db_context,
        interface_type=interface,
        conversation_id=conv_id,
        interface_message_id="user1",
        turn_id=None,
        thread_root_id=1,
        timestamp=now - timedelta(seconds=1),
        role="user",
        content="Initial prompt",
    )
    # Assert that results contain IDs
    assert t1_msg1_result is not None
    assert t1_msg2_result is not None
    assert t1_msg3_result is not None

    # Act
    turn_1_messages = await get_messages_by_turn_id(db_context, turn_1)

    # Assert
    assert len(turn_1_messages) == 3
    assert [m["internal_id"] for m in turn_1_messages] == [
        t1_msg1_result,
        t1_msg2_result,
        t1_msg3_result,
    ]  # Check order
    assert all(m["turn_id"] == turn_1 for m in turn_1_messages)

    # Act: Get messages for a turn with no messages (needs context)
    empty_turn_messages = await get_messages_by_turn_id(db_context, str(uuid.uuid4()))
    # Assert
    assert len(empty_turn_messages) == 0


@pytest.mark.asyncio
async def test_update_message_interface_id_sets_id(db_context: Database) -> None:
    """Verify that the interface message ID can be updated after insertion."""
    # Arrange
    interface = "update_test"
    conv_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    new_interface_id = f"telegram_{uuid.uuid4()}"

    initial_result = await add_message_to_history(
        db_context,
        interface,
        conv_id,
        None,
        str(uuid.uuid4()),
        1,
        now,
        "assistant",
        "Initial content",
    )
    assert initial_result is not None
    internal_id = initial_result

    # Act
    update_successful = await update_message_interface_id(
        db_context, internal_id, new_interface_id
    )

    # Assert
    assert update_successful is True
    result = await db_context.message_history.get_row_by_internal_id(internal_id)
    assert result is not None
    assert result["interface_message_id"] == new_interface_id

    # Act: Try to update non-existent internal ID (needs context)
    update_failed = await update_message_interface_id(db_context, 99999, "some_id")
    # Assert
    assert update_failed is False


@pytest.mark.asyncio
async def test_get_messages_by_thread_id_retrieves_correct_sequence(
    db_context: Database,
) -> None:
    """Verify retrieving all messages for a specific thread_root_id in order."""
    # Arrange
    interface = "thread_test"
    conv_id_1 = str(uuid.uuid4())
    conv_id_2 = str(uuid.uuid4())
    now = datetime.now(UTC)

    # Thread 1 messages
    msg1_result = await add_message_to_history(
        db_context,
        interface_type=interface,
        conversation_id=conv_id_1,
        interface_message_id="msg1",
        turn_id=None,
        thread_root_id=None,
        timestamp=now,
        role="user",
        content="Thread 1 Start",
    )
    assert msg1_result is not None
    thread_1_root = msg1_result  # Use the internal_id of the first message as the root
    msg1_id = thread_1_root  # Keep for assertion later

    msg2_result = await add_message_to_history(
        db_context,
        interface_type=interface,
        conversation_id=conv_id_1,
        interface_message_id=None,
        turn_id="t1",
        thread_root_id=thread_1_root,
        timestamp=now + timedelta(seconds=1),
        role="assistant",
        content="Thread 1 Reply 1",
    )
    assert msg2_result is not None
    msg2_id = msg2_result

    msg3_result = await add_message_to_history(
        db_context,
        interface_type=interface,
        conversation_id=conv_id_1,
        interface_message_id="msg3",
        turn_id=None,
        thread_root_id=thread_1_root,
        timestamp=now + timedelta(seconds=2),
        role="user",
        content="Thread 1 Reply 2",
    )
    assert msg3_result is not None
    msg3_id = msg3_result

    # Thread 2 message (Different conversation, different thread)
    msg4_result = await add_message_to_history(
        db_context,
        interface_type=interface,
        conversation_id=conv_id_2,
        interface_message_id="msg4",
        turn_id=None,
        thread_root_id=None,
        timestamp=now + timedelta(seconds=3),
        role="user",
        content="Thread 2 Start",
    )
    assert msg4_result is not None

    # Act
    thread_1_messages = await get_messages_by_thread_id(db_context, thread_1_root)

    # Assert
    assert len(thread_1_messages) == 3
    assert [m["internal_id"] for m in thread_1_messages] == [
        msg1_id,
        msg2_id,
        msg3_id,
    ]  # Check order
    assert all(
        m["thread_root_id"] == thread_1_root or m["internal_id"] == thread_1_root
        for m in thread_1_messages
    )  # Root msg has NULL thread_root_id

    # Act: Get messages for a thread_root_id that doesn't exist
    # Query for a non-existent ID instead of msg4_id to truly test the empty case
    non_existent_id = 99999
    empty_thread_messages = await get_messages_by_thread_id(db_context, non_existent_id)
    # Assert outside context
    assert len(empty_thread_messages) == 0


@pytest.mark.asyncio
async def test_add_message_without_taint_metadata_logs_regression_guard(
    db_context: Database,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Assistant/tool rows persisted without taint metadata trip the write guard."""
    conversation_id = str(uuid.uuid4())
    repo_logger = "family_assistant.storage.repositories.message_history"

    with caplog.at_level(logging.ERROR, logger=repo_logger):
        assistant_id = await db_context.message_history.add_message(
            AssistantMessage(content="no metadata"),
            interface_type="test_guard",
            conversation_id=conversation_id,
            timestamp=datetime.now(UTC),
        )
        tool_id = await db_context.message_history.add_message(
            ToolMessage(tool_call_id="call_1", content="{}", name="some_tool"),
            interface_type="test_guard",
            conversation_id=conversation_id,
            timestamp=datetime.now(UTC),
        )

    assert assistant_id is not None
    assert tool_id is not None
    guard_records = [
        record
        for record in caplog.records
        if "taint_metadata_missing_at_write" in record.getMessage()
    ]
    assert len(guard_records) == 2

    # Rows with metadata (or non-applicable roles) do not trip the guard.
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger=repo_logger):
        classified_id = await db_context.message_history.add_message(
            AssistantMessage(
                content="with metadata",
                taint_metadata=TurnTaintState.empty().to_metadata(),
            ),
            interface_type="test_guard",
            conversation_id=conversation_id,
            timestamp=datetime.now(UTC),
        )
        system_id = await db_context.message_history.add_message(
            SystemMessage(content="system trigger"),
            interface_type="test_guard",
            conversation_id=conversation_id,
            timestamp=datetime.now(UTC),
        )

    assert classified_id is not None
    assert system_id is not None
    assert not any(
        "taint_metadata_missing_at_write" in record.getMessage()
        for record in caplog.records
    )

    classified_row = await db_context.message_history.get_row_by_internal_id(
        classified_id
    )
    assert classified_row is not None
    assert classified_row["taint_metadata_version"] == "runtime_v2"


@pytest.mark.asyncio
async def test_subconversation_taint_carries_the_delegate_turns_sensitive_reads(
    db_context: Database,
) -> None:
    conversation_id = str(uuid.uuid4())
    subconversation_id = str(uuid.uuid4())
    read = SensitiveReadScope(
        kind="documents", qualifier="search_documents", surfaced_ids=frozenset()
    )
    await db_context.message_history.add_message(
        AssistantMessage(
            content="found it",
            taint_metadata=(
                TurnTaintState
                .empty()
                .add_sensitive_read(read, "model_generated")
                .to_metadata()
            ),
        ),
        interface_type="web",
        conversation_id=conversation_id,
        subconversation_id=subconversation_id,
        timestamp=datetime.now(UTC),
    )

    merged_metadata = (
        await db_context.message_history.get_merged_taint_metadata_for_subconversation(
            interface_type="web",
            conversation_id=conversation_id,
            subconversation_id=subconversation_id,
        )
    )

    merged = TurnTaintState.empty().with_sensitive_reads_from(merged_metadata)
    assert [record.scope for record in merged.sensitive_reads] == [read]


@pytest.mark.asyncio
async def test_subconversation_taint_merges_tool_rows_after_assistant(
    db_context: Database,
) -> None:
    conversation_id = str(uuid.uuid4())
    subconversation_id = str(uuid.uuid4())
    await db_context.message_history.add_message(
        AssistantMessage(
            content="calling a tool",
            taint_metadata=TurnTaintState.empty().to_metadata(),
        ),
        interface_type="web",
        conversation_id=conversation_id,
        subconversation_id=subconversation_id,
        timestamp=datetime.now(UTC),
    )
    untrusted_tool_taint = TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.TOOL_OUTPUT,
            source_id="call_untrusted",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset(),
            reason="untrusted tool output",
        )
    )
    await db_context.message_history.add_message(
        ToolMessage(
            tool_call_id="call_untrusted",
            content="external result",
            name="external_tool",
            taint_metadata=untrusted_tool_taint.to_metadata(),
        ),
        interface_type="web",
        conversation_id=conversation_id,
        subconversation_id=subconversation_id,
        timestamp=datetime.now(UTC),
    )

    merged_metadata = (
        await db_context.message_history.get_merged_taint_metadata_for_subconversation(
            interface_type="web",
            conversation_id=conversation_id,
            subconversation_id=subconversation_id,
        )
    )

    assert merged_metadata is not None
    assert (
        TurnTaintState.from_metadata(merged_metadata).max_tier
        == SourceTrustTier.UNKNOWN_EXTERNAL
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("external_taint", [False, True])
async def test_script_preparation_error_persists_taint_metadata(
    db_engine: AsyncEngine,
    db_context: Database,
    caplog: pytest.LogCaptureFixture,
    external_taint: bool,
) -> None:
    """Preparation errors persist classified history without write or read alarms."""
    now = datetime.now(UTC)
    set_engine_history_taint_epoch(db_engine, now - timedelta(days=1))
    conversation_id = str(uuid.uuid4())
    state = TurnTaintState.empty()
    if external_taint:
        state = state.add_source(
            TaintSource(
                source_type=TaintSourceType.EMAIL,
                source_id="external-message",
                tier=SourceTrustTier.UNKNOWN_EXTERNAL,
                labels=frozenset(),
                reason="External content already present in the turn.",
            )
        )
    provider = TaintTrackingToolsProvider(
        PolicyEnforcingToolsProvider(
            LocalToolsProvider(
                registrations=[
                    item
                    for item in LOCAL_TOOL_REGISTRATIONS
                    if item.definition["function"]["name"] == "execute_script"
                ]
            ),
            PolicyEngine.from_policy_config(
                ToolPolicyConfig(default_decision=ToolPolicyDecision.ALLOW)
            ),
        ),
        taint_policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
    )
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id=conversation_id,
        user_name="Test User",
        turn_id=str(uuid.uuid4()),
        db_context=db_context,
        processing_service=None,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        credential_resolvers=None,
        api_backend=None,
        timezone=ZoneInfo("UTC"),
        taint_tracker=InMemoryTurnTaintTracker(state),
    )

    result = await provider.execute_tool(
        "execute_script", {"script": "def broken("}, context, "script-error"
    )
    assert isinstance(result, ToolResult)
    assert "Syntax error" in result.get_text()
    internal_id = await db_context.message_history.add_message(
        ToolMessage(
            tool_call_id="script-error",
            name="execute_script",
            content=result.get_text(),
            taint_metadata=context.tool_result_taint_metadata.get("script-error"),
        ),
        interface_type="test",
        conversation_id=conversation_id,
        timestamp=now,
    )
    history = await db_context.message_history.get_recent(
        interface_type="test", conversation_id=conversation_id, limit=5
    )

    assert internal_id is not None
    row = await db_context.message_history.get_row_by_internal_id(internal_id)
    assert row is not None
    assert row["taint_metadata_version"] == "runtime_v2"
    assert len(history) == 1
    assert isinstance(history[0], ToolMessage)
    assert history[0].taint_metadata == state.with_authorship_floor().to_metadata()
    assert not any(
        alarm in record.getMessage()
        for record in caplog.records
        for alarm in (
            "taint_metadata_missing_at_write",
            "post_epoch_missing_taint_metadata",
        )
    )
