"""No attachment is refused for its type.

Size is the only bound on what can be stored: a file the model cannot read
directly is still one the assistant can open with its attachment tools, hand to
a profile that can read it, or simply keep for the user. Refusing it at
registration turned "send me your accounts export" into an upload error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from family_assistant.services.attachment_registry import (
    AttachmentRegistry,
    AttachmentTooLargeError,
)
from family_assistant.storage.database import Database

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.fixture
def registry(tmp_path: Path, db_engine: AsyncEngine) -> AttachmentRegistry:
    return AttachmentRegistry(
        storage_path=str(tmp_path / "attachments"), db_engine=db_engine
    )


@pytest.mark.parametrize(
    ("filename", "mime_type"),
    [
        ("bracket.stl", "model/stl"),
        ("accounts.qbo", "application/vnd.intu.qbo"),
        ("dump.bin", "application/octet-stream"),
        ("archive.zip", "application/zip"),
    ],
)
async def test_a_user_can_upload_any_type(
    registry: AttachmentRegistry,
    db_engine: AsyncEngine,
    filename: str,
    mime_type: str,
) -> None:
    metadata = await registry.register_user_attachment(
        db_context=Database(db_engine),
        content=b"\0\1\2\3",
        filename=filename,
        mime_type=mime_type,
    )

    assert metadata.mime_type == mime_type
    assert metadata.size == 4


async def test_a_tool_can_store_any_type(
    registry: AttachmentRegistry, db_engine: AsyncEngine
) -> None:
    """A download tool labels what it fetched, or admits it does not know.

    `application/octet-stream` is the honest answer for bytes off the open web,
    and it was the one answer the registry refused.
    """
    db_context = Database(db_engine)
    metadata = await registry.store_and_register_tool_attachment(
        file_content=b"\0\1\2\3",
        filename="download.dat",
        content_type="application/octet-stream",
        tool_name="download_media",
        db_context=db_context,
    )

    assert metadata.mime_type == "application/octet-stream"

    content = await registry.get_attachment_content(
        db_context, metadata.attachment_id, acting_user_id=None
    )
    assert content == b"\0\1\2\3"


async def test_an_oversized_upload_reports_the_limit(
    tmp_path: Path, db_engine: AsyncEngine
) -> None:
    """The size and the limit are what a user can act on.

    Media is held to the tighter multimodal limit, so a recording admitted by
    the client can still be refused here. Raised as a plain `ValueError` the
    refusal reached the upload route's catch-all and came back as a 500, which
    reads as the server having broken rather than the file being too big.
    """
    registry = AttachmentRegistry(
        storage_path=str(tmp_path / "attachments"),
        db_engine=db_engine,
        config={"max_file_size": 4000, "max_multimodal_size": 1000},
    )

    with pytest.raises(AttachmentTooLargeError, match="maximum allowed size of 1000"):
        await registry.register_user_attachment(
            db_context=Database(db_engine),
            content=b"\0" * 2000,
            filename="long.ogg",
            mime_type="audio/ogg",
        )
