@pytest.mark.asyncio
async def test_add_message_stores_optional_fields(db_context: DatabaseContext):
    """Verify storing messages with optional fields populated."""
    # Arrange
    interface_type = "test_optional"
    conversation_id = str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    thread_root_id = 123  # Assume this ID exists from a previous message
    now = datetime.now(timezone.utc)
    role = "assistant"
    tool_calls_data = [{"id": "call_abc", "type": "function", "function": {"name": "get_weather", "arguments": '{"location": "London"}'}}]
    reasoning_data = {"model": "test-model", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    error_trace = "Something went wrong"
    tool_call_id = "call_abc" # For a potential 'tool' role message

    # Act: Store an assistant message with tool calls and reasoning
    assistant_msg_id = await add_message_to_history(
        db_context=db_context,
        interface_type=interface_type,
        conversation_id=conversation_id,
        interface_message_id=None, # Assistant msg might not have one initially
        turn_id=turn_id,
        thread_root_id=thread_root_id,
        timestamp=now,
        role=role,
        content="Calling tool...",
        tool_calls=tool_calls_data,
        reasoning_info=reasoning_data,
    )
    # Act: Store a tool response message
    tool_msg_id = await add_message_to_history(
        db_context=db_context,
        interface_type=interface_type,
        conversation_id=conversation_id,
        interface_message_id=None,
        turn_id=turn_id,
        thread_root_id=thread_root_id,
        timestamp=now + timedelta(milliseconds=100),
        role="tool",
        content="Weather is sunny",
        tool_call_id=tool_call_id,
        error_traceback=error_trace, # Can store traceback even for non-error roles if needed
    )

    # Assert Assistant Message
    async with db_context as ctx:
        assistant_result = await ctx.fetch_one(
            text("SELECT * FROM message_history WHERE internal_id = :id"),
            {"id": assistant_msg_id},
        )
    assert assistant_result is not None
    assert assistant_result["turn_id"] == turn_id
    assert assistant_result["thread_root_id"] == thread_root_id
    assert assistant_result["tool_calls"] == tool_calls_data # Check JSON storage
    assert assistant_result["reasoning_info"] == reasoning_data
    assert assistant_result["tool_call_id"] is None # Assistant doesn't have tool_call_id
    assert assistant_result["error_traceback"] is None

    # Assert Tool Message
    async with db_context as ctx:
        tool_result = await ctx.fetch_one(
            text("SELECT * FROM message_history WHERE internal_id = :id"),
            {"id": tool_msg_id},
        )
    assert tool_result is not None
    assert tool_result["turn_id"] == turn_id
    assert tool_result["thread_root_id"] == thread_root_id
    assert tool_result["tool_call_id"] == tool_call_id
    assert tool_result["error_traceback"] == error_trace
    assert tool_result["tool_calls"] is None
    assert tool_result["reasoning_info"] is None


@pytest.mark.asyncio
async def test_get_recent_history_retrieves_correct_messages(db_context: DatabaseContext):
    """Verify get_recent_history filters, limits, orders, and handles age correctly."""
    # Arrange
    interface = "history_test"
    conv_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    # Add messages with varying timestamps
    msg1_id = await add_message_to_history(db_context, interface, conv_id, "msg1", None, None, now - timedelta(minutes=10), "user", "Old message")
    msg2_id = await add_message_to_history(db_context, interface, conv_id, "msg2", None, None, now - timedelta(minutes=2), "assistant", "Recent 1")
    msg3_id = await add_message_to_history(db_context, interface, conv_id, "msg3", None, None, now - timedelta(minutes=1), "user", "Recent 2")
    # Add a message for a different conversation
    await add_message_to_history(db_context, interface, "other_conv", "msg_other", None, None, now, "user", "Other convo")

    # Act: Get recent history with limit and age cutoff
    recent_messages = await get_recent_history(
        db_context,
        interface_type=interface,
        conversation_id=conv_id,
        limit=2,
        max_age=timedelta(minutes=5) # Should exclude msg1
    )

    # Assert
    assert len(recent_messages) == 2 # Limit respected
    # Check chronological order (oldest first in the returned list)
    assert recent_messages[0]["internal_id"] == msg2_id
    assert recent_messages[1]["internal_id"] == msg3_id
    assert recent_messages[0]["content"] == "Recent 1"
    assert recent_messages[1]["content"] == "Recent 2"
    # Verify msg1 (too old) and msg_other (different convo) are not included
    assert all(msg["internal_id"] != msg1_id for msg in recent_messages)


@pytest.mark.asyncio
async def test_get_message_by_interface_id_retrieval(db_context: DatabaseContext):
    """Verify retrieving a specific message by its interface identifiers."""
    # Arrange
    interface = "get_by_id"
    conv_id = str(uuid.uuid4())
    msg_id = "message_abc"
    now = datetime.now(timezone.utc)
    content = "Target message"
    internal_id = await add_message_to_history(db_context, interface, conv_id, msg_id, None, None, now, "user", content)

    # Act: Retrieve the message
    retrieved_message = await get_message_by_interface_id(db_context, interface, conv_id, msg_id)

    # Assert
    assert retrieved_message is not None
    assert retrieved_message["internal_id"] == internal_id
    assert retrieved_message["interface_type"] == interface
    assert retrieved_message["conversation_id"] == conv_id
    assert retrieved_message["interface_message_id"] == msg_id
    assert retrieved_message["content"] == content

    # Act: Try to retrieve non-existent message
    not_found_message = await get_message_by_interface_id(db_context, interface, conv_id, "non_existent_id")

    # Assert
    assert not_found_message is None


@pytest.mark.asyncio
async def test_get_messages_by_turn_id_retrieves_correct_sequence(db_context: DatabaseContext):
    """Verify retrieving all messages for a specific turn_id in order."""
     # Arrange
    interface = "turn_test"
    conv_id = str(uuid.uuid4())
    turn_1 = str(uuid.uuid4())
    turn_2 = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    # Turn 1 messages
    t1_msg1 = await add_message_to_history(db_context, interface, conv_id, None, turn_1, 1, now, "assistant", "T1 Call tool")
    t1_msg2 = await add_message_to_history(db_context, interface, conv_id, None, turn_1, 1, now + timedelta(seconds=1), "tool", "T1 Tool result")
    t1_msg3 = await add_message_to_history(db_context, interface, conv_id, None, turn_1, 1, now + timedelta(seconds=2), "assistant", "T1 Final answer")
    # Turn 2 message
    t2_msg1 = await add_message_to_history(db_context, interface, conv_id, None, turn_2, 1, now + timedelta(seconds=3), "assistant", "T2 Different turn")
    # Message with no turn id
    no_turn_msg = await add_message_to_history(db_context, interface, conv_id, "user1", None, 1, now - timedelta(seconds=1), "user", "Initial prompt")

    # Act
    turn_1_messages = await get_messages_by_turn_id(db_context, turn_1)

    # Assert
    assert len(turn_1_messages) == 3
    assert [m["internal_id"] for m in turn_1_messages] == [t1_msg1, t1_msg2, t1_msg3] # Check order
    assert all(m["turn_id"] == turn_1 for m in turn_1_messages)

    # Act: Get messages for a turn with no messages
    empty_turn_messages = await get_messages_by_turn_id(db_context, str(uuid.uuid4()))
    # Assert
    assert len(empty_turn_messages) == 0


@pytest.mark.asyncio
async def test_update_message_interface_id_sets_id(db_context: DatabaseContext):
    """Verify that the interface message ID can be updated after insertion."""
    # Arrange
    interface = "update_test"
    conv_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    internal_id = await add_message_to_history(
        db_context, interface, conv_id, None, str(uuid.uuid4()), 1, now, "assistant", "Initial content"
    )
    assert internal_id is not None
    new_interface_id = f"telegram_{uuid.uuid4()}"

    # Act
    update_successful = await update_message_interface_id(db_context, internal_id, new_interface_id)

    # Assert
    assert update_successful is True
    # Verify directly in the DB
    async with db_context as ctx:
        result = await ctx.fetch_one(
            text("SELECT interface_message_id FROM message_history WHERE internal_id = :id"),
            {"id": internal_id},
        )
    assert result is not None
    assert result["interface_message_id"] == new_interface_id

    # Act: Try to update non-existent internal ID
    update_failed = await update_message_interface_id(db_context, 99999, "some_id")
    # Assert
    assert update_failed is False
