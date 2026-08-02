"""Size limits applied when an attachment is registered.

`max_file_size` bounds anything stored; `max_multimodal_size` bounds what can
only reach a model as media. Media between the two limits would otherwise be
stored, read back in full and rejected by the provider mid-turn, so the tighter
bound is applied at registration where the size can be reported to the user.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

_MAX_FILE_SIZE = 4000
_MAX_MULTIMODAL_SIZE = 1000
_BETWEEN_THE_LIMITS = b"\0" * 2000


@pytest.fixture
def registry(tmp_path: Path, db_engine: AsyncEngine) -> AttachmentRegistry:
    return AttachmentRegistry(
        storage_path=str(tmp_path / "attachments"),
        db_engine=db_engine,
        config={
            "max_file_size": _MAX_FILE_SIZE,
            "max_multimodal_size": _MAX_MULTIMODAL_SIZE,
        },
    )


@pytest.mark.parametrize(
    ("mime_type", "expected"),
    [
        ("image/png", _MAX_MULTIMODAL_SIZE),
        ("audio/ogg", _MAX_MULTIMODAL_SIZE),
        ("video/mp4", _MAX_MULTIMODAL_SIZE),
        ("application/pdf", _MAX_FILE_SIZE),
        ("text/plain", _MAX_FILE_SIZE),
        (None, _MAX_FILE_SIZE),
    ],
)
def test_media_is_held_to_the_multimodal_limit(
    registry: AttachmentRegistry, mime_type: str | None, expected: int
) -> None:
    assert registry.size_limit_for_mime(mime_type) == expected


async def test_registering_oversized_media_reports_the_multimodal_limit(
    registry: AttachmentRegistry, db_engine: AsyncEngine
) -> None:
    db_context = Database(db_engine)
    with pytest.raises(
        ValueError, match=f"maximum allowed size of {_MAX_MULTIMODAL_SIZE}"
    ):
        await registry.register_user_attachment(
            db_context=db_context,
            content=_BETWEEN_THE_LIMITS,
            filename="long.ogg",
            mime_type="audio/ogg",
        )


async def test_a_document_between_the_limits_is_still_accepted(
    registry: AttachmentRegistry, db_engine: AsyncEngine
) -> None:
    """The tighter bound applies to media only.

    Not because a PDF never reaches a model as bytes -- on the Responses API it
    now does -- but because it has a use that needs no model at all:
    `read_text_attachment` extracts its text. A size only a model objects to is
    not a reason to refuse the upload, so `max_file_size` remains its limit.
    """
    db_context = Database(db_engine)
    metadata = await registry.register_user_attachment(
        db_context=db_context,
        content=_BETWEEN_THE_LIMITS,
        filename="long.pdf",
        mime_type="application/pdf",
    )

    assert metadata.size == len(_BETWEEN_THE_LIMITS)


async def test_a_generated_attachment_is_not_held_to_the_media_limit(
    registry: AttachmentRegistry, db_engine: AsyncEngine
) -> None:
    """A tool's output is the result the user asked for, not input awaiting a model.

    Applying the media bound here would discard a generated video to protect a
    later injection that may never happen — the user loses the work over a limit
    describing what a model will accept. `max_file_size` still applies.
    """
    db_context = Database(db_engine)
    metadata = await registry.store_and_register_tool_attachment(
        file_content=_BETWEEN_THE_LIMITS,
        filename="generated.mp4",
        content_type="video/mp4",
        tool_name="generate_video",
        db_context=db_context,
    )

    assert metadata.size == len(_BETWEEN_THE_LIMITS)


async def test_a_generated_attachment_still_obeys_the_file_limit(
    registry: AttachmentRegistry, db_engine: AsyncEngine
) -> None:
    db_context = Database(db_engine)
    with pytest.raises(ValueError, match=f"maximum allowed size of {_MAX_FILE_SIZE}"):
        await registry.store_and_register_tool_attachment(
            file_content=b"\0" * (_MAX_FILE_SIZE + 1),
            filename="huge.mp4",
            content_type="video/mp4",
            tool_name="generate_video",
            db_context=db_context,
        )
