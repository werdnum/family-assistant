"""Telegram voice notes have to reach the handler at all.

A voice note is a distinct Telegram type: it arrives as `message.voice`, not
`message.audio`, and `filters.AUDIO` does not match it. While `filters.VOICE` was
unregistered the update was never routed to `message_handler`, so no attachment
was created and the transcription handoff could not run — for the everyday way a
person sends speech, which is what that handoff exists for.

Asserted on the registered filter rather than end to end because the Telegram
mock server exposes no `sendVoice`; this pins the defect that was actually
present (the update not being routed) rather than approximating it.
"""

from typing import TYPE_CHECKING

from telegram.ext import MessageHandler, filters

if TYPE_CHECKING:
    from tests.functional.telegram.conftest import TelegramHandlerTestFixture


def _message_handler_filters(
    fixture: "TelegramHandlerTestFixture",
) -> list[filters.BaseFilter]:
    fixture.handler.register_handlers()
    return [
        handler.filters
        for group in fixture.application.handlers.values()
        for handler in group
        if isinstance(handler, MessageHandler) and handler.filters is not None
    ]


def test_voice_notes_are_routed_to_the_message_handler(
    telegram_handler_fixture: "TelegramHandlerTestFixture",
) -> None:
    """`filters.VOICE` must be registered, or voice notes never arrive."""
    registered = _message_handler_filters(telegram_handler_fixture)

    assert any("VOICE" in repr(f) for f in registered), (
        "no MessageHandler matches filters.VOICE, so a Telegram voice note is "
        "never routed and cannot be transcribed"
    )


def test_audio_files_are_still_routed(
    telegram_handler_fixture: "TelegramHandlerTestFixture",
) -> None:
    """Adding VOICE must not displace AUDIO — they are different message types."""
    registered = _message_handler_filters(telegram_handler_fixture)

    assert any("AUDIO" in repr(f) for f in registered)
