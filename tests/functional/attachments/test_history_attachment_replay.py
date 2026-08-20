"""How a stored attachment comes back on a later turn.

Replaying a message means deciding, per attachment, between handing the model
the bytes and naming the file. The MIME type decides: an adapter given inline
bytes of a type it cannot read either rejects the request or drops the file,
and the model then answers about a file it never received.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from family_assistant.llm.messages import (
    ImageUrlContentPart,
    MessageAttachmentMetadata,
    TextContentPart,
    UserMessage,
)
from family_assistant.storage.database import Database

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

CONVERSATION_ID = "replay-conversation"


async def _replay(
    db_engine: AsyncEngine, attachments: list[MessageAttachmentMetadata]
) -> UserMessage:
    db = Database(db_engine)
    await db.message_history.add_message(
        UserMessage(content="What is this?"),
        interface_type="web",
        conversation_id=CONVERSATION_ID,
        timestamp=datetime.now(UTC),
        attachments=attachments,
    )
    messages = await db.message_history.get_recent(
        interface_type="web", conversation_id=CONVERSATION_ID
    )
    replayed = messages[0]
    assert isinstance(replayed, UserMessage)
    return replayed


@pytest.mark.parametrize(
    "mime_type",
    ["image/png", "audio/ogg", "video/mp4", "application/pdf", "text/plain"],
)
async def test_bytes_a_provider_reads_are_replayed_inline(
    db_engine: AsyncEngine, mime_type: str
) -> None:
    replayed = await _replay(
        db_engine,
        [
            MessageAttachmentMetadata(
                type="document",
                attachment_id="att-native",
                content_url="/api/attachments/att-native",
                mime_type=mime_type,
                filename="file",
            )
        ],
    )

    assert isinstance(replayed.content, list)
    assert any(isinstance(part, ImageUrlContentPart) for part in replayed.content)


async def test_a_type_no_provider_takes_inline_is_named_instead(
    db_engine: AsyncEngine,
) -> None:
    """The id has to survive: it is how the assistant opens the file later."""
    replayed = await _replay(
        db_engine,
        [
            MessageAttachmentMetadata(
                type="document",
                attachment_id="att-model",
                content_url="/api/attachments/att-model",
                mime_type="model/stl",
                filename="bracket.stl",
            )
        ],
    )

    assert isinstance(replayed.content, list)
    assert not any(isinstance(part, ImageUrlContentPart) for part in replayed.content)
    described = " ".join(
        part.text for part in replayed.content if isinstance(part, TextContentPart)
    )
    assert "bracket.stl" in described
    assert "model/stl" in described
    assert "att-model" in described


async def test_a_generically_labelled_attachment_still_comes_back(
    db_engine: AsyncEngine,
) -> None:
    """The label is not what decides -- an older client's 'file' is replayed too.

    Keying the replay on the stored label dropped whatever a client had no
    case for, so a later turn ran as though the file had never been sent.
    """
    replayed = await _replay(
        db_engine,
        [
            MessageAttachmentMetadata(
                type="file",
                attachment_id="att-image",
                content_url="/api/attachments/att-image",
                mime_type="image/png",
                filename="photo.png",
            )
        ],
    )

    assert isinstance(replayed.content, list)
    assert any(isinstance(part, ImageUrlContentPart) for part in replayed.content)
