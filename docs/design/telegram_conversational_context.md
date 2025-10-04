# Telegram Conversational Context and Attachment Handling

## Problem Statement

Users experienced attachment context loss when referencing images or files from earlier messages in
a Telegram conversation thread. For example:

1. User sends an image of a bird statue
2. User replies to that message: "Can you highlight the eagle in this image?"
3. The assistant couldn't access the original image because the root message was excluded from
   thread queries

This resulted in the assistant hallucinating invalid attachment IDs or claiming it couldn't find the
referenced attachment.

## Root Cause Analysis

The issue had two main components:

### 1. Thread Query Excluded Root Message

The `get_by_thread_id()` method in `message_history.py` only retrieved messages where
`thread_root_id` matched the specified ID:

```python
# OLD (broken):
stmt = (
    select(message_history_table)
    .where(message_history_table.c.thread_root_id == thread_root_id)
    .order_by(message_history_table.c.timestamp.asc())
)
```

This excluded the root message itself because root messages have `thread_root_id=NULL`. Any
attachments in the root message were therefore invisible to the assistant.

### 2. Limited Message History

The `max_history_messages` setting was only 3, which meant even when the root message was included,
longer conversations would still lose context.

## Solution Implementation

### 1. Fixed Thread Query to Include Root Message

Modified `get_by_thread_id()` to use an OR condition that matches both the root message and its
children:

```python
# NEW (fixed):
conditions = [
    or_(
        message_history_table.c.internal_id == thread_root_id,
        message_history_table.c.thread_root_id == thread_root_id,
    )
]
```

This ensures the root message is always included in thread history, making its attachments
available.

**Test Coverage:**

- `tests/functional/telegram/test_thread_history.py` - Comprehensive tests for thread queries
- `tests/functional/test_processing_profiles.py` - Updated to expect root message in history

### 2. Increased Message History Limit

Changed `max_history_messages` from 3 to 10 in `config.yaml` to provide better context for
conversations with multiple messages. The `history_max_age_hours` remains at 2 hours.

### 3. Implemented Telegram Media Groups

When multiple consecutive images are sent, they are now grouped into a Telegram media group instead
of being sent separately. This provides:

- Better user experience (images appear together)
- Proper threading support (users can reply to the entire group)
- Clearer attachment references in conversation history

**Implementation Details:**

- Modified `_send_attachments()` in `telegram_bot.py`
- Groups consecutive images using `send_media_group()` API
- Single images still sent via `send_photo()`
- Non-images sent via `send_document()`
- Media groups limited to images only (Telegram API restriction)

**Test Coverage:**

- `tests/functional/telegram/test_telegram_media_groups.py`:
  - Multiple images sent as media group
  - Single image sent individually
  - Mixed attachments (images + documents) handled correctly
  - Media groups with reply_to_message_id

## How Telegram Threading Works

### Message Structure

In Telegram, when a user replies to a message, the reply contains a `reply_to_message` reference.
The bot tracks this to maintain conversation threads:

1. **Root Message**: First message in a thread (has `thread_root_id=NULL`)
2. **Reply Messages**: Messages replying to the root or other messages in the thread (have
   `thread_root_id` set to the root message's internal ID)

### Thread History Retrieval

When a user sends a reply, the system:

1. Identifies the root message from `reply_to_message_id`
2. Retrieves all messages in the thread using `get_by_thread_id()`
3. Now correctly includes the root message (with attachments) in the history
4. Passes this full context to the LLM

## Attachment ID Injection System

Tools that generate attachments automatically inject attachment ID markers into their responses:

```
[Attachment ID: <uuid>]
```

These markers are:

- Stored in the message history
- Visible to the LLM in conversation context
- Used by the LLM to reference attachments with tools like `attach_to_response`

When the root message is included in thread history, all its attachment ID markers are available for
the LLM to use.

## User Experience Improvements

### Before Fix

```
User: [sends bird.jpg]
Bot: Here's your image
User: (replies) Can you highlight the eagle?
Bot: I don't see any image attached. Please send the image.
```

### After Fix

```
User: [sends bird.jpg]
Bot: Here's your image
User: (replies) Can you highlight the eagle?
Bot: [uses image from thread history to highlight the eagle]
```

## Attachment Context Provider (Implemented)

The system now dynamically extracts and includes attachment context from conversations:

```
Recent Attachments in Conversation:
- [uuid-1] image.jpg (image/jpeg) - 5 minutes ago
- [uuid-2] document.pdf (application/pdf) - 2 hours ago
```

**Implementation Details:**

- `_extract_conversation_attachments_context()` queries the `attachment_metadata` table directly by
  `conversation_id` using the storage layer method `get_recent_attachments_for_conversation()`
- Uses time-based filtering (configurable `max_age_hours`, defaults to `history_max_age_hours`)
- Fetches attachment metadata from the AttachmentRegistry
- Formats context with time-based age strings ("X hours/minutes ago") using template from
  `prompts.yaml`
- Injected into system prompt alongside other context providers
- Only appears when replying in a thread (Telegram) and attachments exist in the conversation

**Benefits:**

- Makes attachment availability explicit to the LLM
- Reduces hallucination of invalid attachment IDs
- Provides clear context about what files are available

**Test Coverage:**

- `tests/functional/telegram/test_thread_history.py::test_attachment_context_extraction`

## Future Enhancements

### Message Concatenation Investigation

Some Telegram interfaces concatenate rapid messages. Need to investigate:

- How this affects thread tracking
- Whether concatenated messages preserve attachment references
- If additional handling is needed

## Implementation Commits

1. **fix: Include root message in thread queries for attachment context** (76b12d2c)

   - Modified thread query logic
   - Added comprehensive tests

2. **feat: Implement Telegram Media Groups for consecutive images** (3449884f)

   - Implemented media group functionality
   - Added test coverage

3. **config: Increase message history limit from 3 to 10 messages** (887af94d)

   - Updated configuration

## References

- `src/family_assistant/storage/repositories/message_history.py:get_by_thread_id()`
- `src/family_assistant/telegram_bot.py:_send_attachments()`
- `tests/functional/telegram/test_thread_history.py`
- `tests/functional/telegram/test_telegram_media_groups.py`
