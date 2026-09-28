"""Telegram voice notes have to reach the handler at all.

A voice note is a distinct Telegram type: it arrives as `message.voice`, not
`message.audio`, and `filters.AUDIO` does not match it. While `filters.VOICE` was
unregistered the update was never routed to `message_handler`, so no attachment
was created and the transcription handoff could not run — for the everyday way a
person sends speech, which is what that handoff exists for.

Builds real `Update` objects carrying a `voice`/`audio` payload and asserts that
the registered `MessageHandler` whose callback is `message_handler` actually
accepts them (`check_update`), rather than pattern-matching the filter's repr --
a filter combination such as `filters.TEXT & ~filters.VOICE` would satisfy a
repr search for "VOICE" while routing no voice notes at all.

Still short of end to end because the Telegram mock server exposes no
`sendVoice`, so nothing here calls the live bot.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from telegram import Audio, Chat, Message, Update, User, Voice
from telegram.ext import MessageHandler

if TYPE_CHECKING:
    from tests.functional.telegram.conftest import TelegramHandlerTestFixture


def _message_handler(
    fixture: "TelegramHandlerTestFixture",
) -> MessageHandler:
    matches = [
        handler
        for group in fixture.application.handlers.values()
        for handler in group
        if isinstance(handler, MessageHandler)
        and handler.callback == fixture.handler.message_handler
    ]
    assert len(matches) == 1, (
        f"expected exactly one MessageHandler wired to message_handler, found "
        f"{len(matches)}"
    )
    return matches[0]


def _build_update(*, voice: Voice | None = None, audio: Audio | None = None) -> Update:
    user = User(id=12345, first_name="TestUser", is_bot=False)
    chat = Chat(id=123, type="private")
    message = Message(
        message_id=101,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        voice=voice,
        audio=audio,
    )
    return Update(update_id=1, message=message)


def test_voice_notes_are_routed_to_the_message_handler(
    telegram_handler_fixture: "TelegramHandlerTestFixture",
) -> None:
    """A voice-note update must be accepted by the registered message handler."""
    message_handler = _message_handler(telegram_handler_fixture)
    voice_update = _build_update(
        voice=Voice(file_id="voice1", file_unique_id="voice1", duration=3)
    )

    assert message_handler.check_update(voice_update), (
        "the registered MessageHandler does not accept a voice-note update, so it "
        "is never routed to message_handler and cannot be transcribed"
    )


def test_audio_files_are_still_routed(
    telegram_handler_fixture: "TelegramHandlerTestFixture",
) -> None:
    """Adding VOICE must not displace AUDIO — they are different message types."""
    message_handler = _message_handler(telegram_handler_fixture)
    audio_update = _build_update(
        audio=Audio(file_id="audio1", file_unique_id="audio1", duration=3)
    )

    assert message_handler.check_update(audio_update), (
        "the registered MessageHandler no longer accepts an audio-file update"
    )
