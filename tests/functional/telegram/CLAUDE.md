# Telegram Bot Testing Guide

This file provides guidance for working with Telegram bot tests.

## Telegram Bot Testing Fixture

**`telegram_handler_fixture`** (function scope)

Comprehensive fixture for testing Telegram bot functionality. Located in
`tests/functional/telegram/conftest.py`.

Returns `TelegramHandlerTestFixture` named tuple with:

- `assistant`: Configured Assistant instance
- `handler`: TelegramUpdateHandler
- `mock_bot`: Mocked Telegram bot (AsyncMock)
- `mock_llm`: RuleBasedMockLLMClient for controlled LLM responses
- `mock_confirmation_manager`: Mock for tool confirmation requests
- `mock_application`: Mock Telegram Application
- `processing_service`: Configured ProcessingService
- `tools_provider`: Configured ToolsProvider
- `get_db_context_func`: Function to get database context

## Usage Example

```python
async def test_telegram_command(telegram_handler_fixture):
    fixture = telegram_handler_fixture

    # Add LLM rules to control responses
    fixture.mock_llm.rules.append((matcher_func, LLMOutput(...)))

    # Test handler methods
    await fixture.handler.handle_message(update, context)

    # Assert bot interactions
    fixture.mock_bot.send_message.assert_called_once()
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

    # Create mock update for /start command
    update = create_update_with_command("/start")
    context = create_context()

    # Execute command
    await fixture.handler.start_command(update, context)

    # Verify bot response
    fixture.mock_bot.send_message.assert_called_once()
    call_args = fixture.mock_bot.send_message.call_args
    assert "Welcome" in call_args[1]["text"]
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

    # Create mock message
    update = create_update_with_message("Hello bot")
    context = create_context()

    # Handle message
    await fixture.handler.handle_message(update, context)

    # Verify interactions
    fixture.mock_bot.send_message.assert_called()
```

## Testing Tool Confirmations

```python
async def test_tool_confirmation(telegram_handler_fixture):
    fixture = telegram_handler_fixture

    # Set up confirmation manager mock
    fixture.mock_confirmation_manager.request_confirmation.return_value = True

    # Test tool that requires confirmation
    # ... test logic
```
