"""How the model learns the time: a stamp on every user message.

Each user message read back from history is shown with the time its row was
written, in the profile's timezone. The newest stamp is the current time, and
every stamp is rendered from the stored row rather than from the clock, so a
message reads identically on every later request. That is what keeps the
prompt append-only: a clock that lived anywhere else would either change an
earlier part of the prompt on every request or vanish from it on the next one.
See docs/design/append-only-prompt.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from family_assistant.llm.messages import ContentPart, TextContentPart, UserMessage

if TYPE_CHECKING:
    from datetime import datetime
    from zoneinfo import ZoneInfo

MESSAGE_TIME_FORMAT = "%a %Y-%m-%d %H:%M %Z"

MESSAGE_TIME_GUIDANCE = (
    "Each user message begins with a [Sent ...] line giving the time it was sent. "
    "The line is added by the system, not written by the user; the newest one is "
    "the current time. Never include such a line in your reply."
)


def sent_at_label(sent_at: datetime, timezone: ZoneInfo) -> str:
    """The stamp shown ahead of a message sent at *sent_at*."""
    return f"[Sent {sent_at.astimezone(timezone).strftime(MESSAGE_TIME_FORMAT)}]"


def with_sent_at(message: UserMessage, timezone: ZoneInfo) -> UserMessage:
    """Return *message* with its send time rendered into its content.

    Messages that were never persisted carry no ``sent_at`` and come back
    unchanged. The stamp is cleared on the copy, so formatting a message twice
    cannot stamp it twice.
    """
    if message.sent_at is None:
        return message
    label = sent_at_label(message.sent_at, timezone)
    if isinstance(message.content, str):
        content: str | list[ContentPart] = f"{label}\n{message.content}"
    else:
        content = [TextContentPart(type="text", text=label), *message.content]
    return message.model_copy(update={"content": content, "sent_at": None})
