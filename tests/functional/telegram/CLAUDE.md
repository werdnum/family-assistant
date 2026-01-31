# Telegram Bot Testing Guide

This file provides guidance for working with Telegram bot tests.

## Testing Architecture

Telegram bot tests use `telegram-bot-api-mock`, a Python mock server that provides realistic
HTTP-level testing. The bot makes real HTTP calls to the mock server, which records all messages and
allows simulating user input.

## Telegram Bot Testing Fixture

**`telegram_handler_fixture`** (function scope)

Comprehensive fixture for testing Telegram bot functionality. Located in
`tests/functional/telegram/conftest.py`.

Returns `TelegramHandlerTestFixture` named tuple with:

- `assistant`: Configured Assistant instance
- `handler`: TelegramUpdateHandler
- `bot`: Real Bot instance connected to telegram-bot-api-mock
- `mock_llm`: RuleBasedMockLLMClient for controlled LLM responses
- `mock_confirmation_manager`: Mock for tool confirmation requests
- `application`: Real Telegram Application connected to mock server
- `processing_service`: Configured ProcessingService
- `tools_provider`: Configured ToolsProvider
- `get_db_context_func`: Function to get database context
- `telegram_client`: TelegramTestClient for simulating user input

## Usage Example

```python
async def test_telegram_command(telegram_handler_fixture):
    fixture = telegram_handler_fixture

    # Add LLM rules to control responses
    fixture.mock_llm.rules.append((matcher_func, LLMOutput(...)))

    # Send a message via the test client (simulates user input)
    result = await fixture.telegram_client.send_message("Hello bot!")

    # Parse the response into an Update and call the handler
    update = Update.de_json(result.get("result", {}), fixture.bot)
    context = create_mock_context(fixture.application)
    await fixture.handler.message_handler(update, context)

    # Check bot responses
    updates = await fixture.telegram_client.get_updates()
```

## Sending Media Attachments

The test client supports sending various media types:

```python
# Send a photo
result = await fixture.telegram_client.send_photo(
    photo_content=photo_bytes,
    filename="image.png",
    caption="What's in this photo?"
)

# Send a video
result = await fixture.telegram_client.send_video(
    video_content=video_bytes,
    filename="video.mp4",
    caption="Describe this video"
)

# Send an audio file
result = await fixture.telegram_client.send_audio(
    audio_content=audio_bytes,
    filename="audio.mp3",
    caption="What song is this?"
)

# Send a document
result = await fixture.telegram_client.send_document(
    document_content=pdf_bytes,
    filename="document.pdf",
    mime_type="application/pdf",
    caption="Summarize this document"
)
```

## Setting Up Mock LLM Responses

The `RuleBasedMockLLMClient` allows you to define rules that match against LLM inputs and return
specific outputs:

```python
# Define a matcher function
def matcher_func(args):
    return "weather" in args["messages"][0]["content"]

# Add rule to fixture
fixture.mock_llm.rules.append((
    matcher_func,
    LLMOutput(content="It's sunny today!")
))
```

## Testing Bot Commands

```python
async def test_start_command(telegram_handler_fixture):
    fixture = telegram_handler_fixture

    # Send a command via the test client
    result = await fixture.telegram_client.send_command("/start")

    # Parse and handle the update
    update = Update.de_json(result.get("result", {}), fixture.bot)
    context = create_mock_context(fixture.application)
    await fixture.handler.start_command(update, context)

    # Check bot responses
    updates = await fixture.telegram_client.get_updates()
    assert len(updates) > 0
```

## Testing Message Handlers

```python
async def test_message_handler(telegram_handler_fixture):
    fixture = telegram_handler_fixture

    # Set up LLM response
    fixture.mock_llm.rules.append((
        lambda args: True,  # Match all
        LLMOutput(content="I understand your message")
    ))

    # Send a message via the test client
    result = await fixture.telegram_client.send_message("Hello bot")
    update = Update.de_json(result.get("result", {}), fixture.bot)
    context = create_mock_context(fixture.application)

    # Handle message
    await fixture.handler.message_handler(update, context)

    # Verify bot sent a response
    updates = await fixture.telegram_client.get_updates()
    assert len(updates) > 0
```

## Testing Tool Confirmations

```python
async def test_tool_confirmation(telegram_handler_fixture):
    fixture = telegram_handler_fixture

    # Set up confirmation manager mock to auto-approve
    fixture.mock_confirmation_manager.return_value = True

    # Test tool that requires confirmation
    # ... test logic
```

## Key Differences from Mocked Approach

The `telegram-bot-api-mock` approach provides:

1. **Real HTTP calls**: The bot makes actual HTTP requests to the mock server
2. **File downloads work**: `get_file()` and `download_to_memory()` work without mocking
3. **Message history**: The mock server records all messages for verification
4. **Realistic testing**: Tests more closely match production behavior

The mock server is session-scoped (`telegram_test_server_session`), so all tests share the same
server instance. Each test gets its own fixture with a fresh handler and client.
