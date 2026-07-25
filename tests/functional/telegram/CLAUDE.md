# Telegram Bot Testing Guide

Tests use `telegram-bot-api-mock`, a Python mock server the bot talks to over real HTTP. Because the
calls are real, `get_file()` / `download_to_memory()` work without any mocking, and the server
records every message the bot sent so you can assert on it. The server is session-scoped
(`telegram_test_server_session`); each test gets a fresh handler and client on top of it.

## `telegram_handler_fixture`

Function-scoped, defined in `tests/functional/telegram/conftest.py`. Yields a
`TelegramHandlerTestFixture` named tuple:

- `assistant` — Assistant instance
- `handler` — `TelegramUpdateHandler`
- `bot` — real `Bot` pointed at the mock server
- `mock_llm` — `RuleBasedMockLLMClient`
- `mock_confirmation_manager` — `AsyncMock` replacing `confirmation_manager.request_confirmation`
- `application` — real Telegram `Application`
- `processing_service` / `tools_provider` — the assistant's defaults
- `get_db_context_func` — async context manager factory for `DatabaseContext`
- `telegram_client` — `TelegramTestClient` (`tests/mocks/telegram_test_server.py`) for simulating
  user input

## Basic Flow

Send input through `telegram_client`, deserialize the mock server's response into an `Update`, then
call the handler yourself:

```python
async def test_message_handler(telegram_handler_fixture):
    fixture = telegram_handler_fixture
    fixture.mock_llm.rules.append((
        lambda args: "weather" in args["messages"][0]["content"],
        LLMOutput(content="It's sunny today!"),
    ))

    result = await fixture.telegram_client.send_message("What's the weather?")
    update = Update.de_json(result.get("result", {}), fixture.bot)
    context = create_context(fixture.application)
    await fixture.handler.message_handler(update, context)

    updates = await fixture.telegram_client.get_updates()
```

There is no shared `create_context` helper — each test file defines its own small builder over
`ContextTypes.DEFAULT_TYPE(application=..., chat_id=..., user_id=...)`. Shared assertion helpers do
exist in `tests/functional/telegram/helpers.py`: `wait_for_bot_response`, `assert_bot_sent_message`,
`assert_bot_sent_message_with_keyboard`, and `extract_callback_data_from_keyboard`.

Slash commands go through `handler.handle_generic_slash_command` / `handler.handle_unknown_command`;
send them with `telegram_client.send_command("/foo")`.

## Media

`telegram_client` has `send_photo`, `send_video`, `send_audio`, and `send_document`. Each takes the
raw bytes plus `filename` and an optional `caption`; `send_document` also takes `mime_type`.

## Tool Confirmations

`mock_confirmation_manager` stands in for `request_confirmation`, so set its `return_value` to a
`ConfirmationOutcome` (e.g. `ConfirmationOutcome(kind="approved")` or `kind="rejected"`), or a
`side_effect` of `asyncio.TimeoutError` to exercise the timeout path.
