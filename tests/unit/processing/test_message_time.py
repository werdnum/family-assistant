"""The send-time stamp that tells the model when each message was sent."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from family_assistant.llm.messages import TextContentPart, UserMessage
from family_assistant.processing.message_time import (
    strip_sent_at_label,
    with_sent_at,
)

pytestmark = pytest.mark.no_db

SYDNEY = ZoneInfo("Australia/Sydney")
SENT = datetime(2026, 10, 3, 23, 54, tzinfo=UTC)


def test_text_message_gets_its_send_time_in_the_profile_timezone() -> None:
    stamped = with_sent_at(UserMessage(content="Hello", sent_at=SENT), SYDNEY)

    assert stamped.content == "[Sent Sun 2026-10-04 10:54 AEDT]\nHello"


def test_multipart_message_gets_a_leading_text_part() -> None:
    message = UserMessage(
        content=[TextContentPart(type="text", text="Look at this")], sent_at=SENT
    )

    stamped = with_sent_at(message, SYDNEY)

    assert isinstance(stamped.content, list)
    assert stamped.content[0] == TextContentPart(
        type="text", text="[Sent Sun 2026-10-04 10:54 AEDT]"
    )
    assert stamped.content[1:] == message.content


def test_the_same_row_renders_identically_every_time() -> None:
    """What makes the stamp safe in a cached, replayed prefix."""
    message = UserMessage(content="Hello", sent_at=SENT)

    assert with_sent_at(message, SYDNEY) == with_sent_at(message, SYDNEY)


def test_stamping_twice_does_not_stamp_twice() -> None:
    once = with_sent_at(UserMessage(content="Hello", sent_at=SENT), SYDNEY)

    assert with_sent_at(once, SYDNEY) == once


def test_unpersisted_messages_are_left_alone() -> None:
    message = UserMessage(content="Hello")

    assert with_sent_at(message, SYDNEY) is message


def test_stripping_recovers_the_message() -> None:
    stamped = with_sent_at(UserMessage(content="Hello\nworld", sent_at=SENT), SYDNEY)

    assert isinstance(stamped.content, str)
    assert strip_sent_at_label(stamped.content) == "Hello\nworld"
    assert strip_sent_at_label("[Sent by Alice] hi") == "[Sent by Alice] hi"
