"""Pure converters between A2A protocol types and FA internal types.

Everything here is I/O-free. Attachments deliberately do not belong: moving
their bytes across the boundary needs the attachment registry, and lives in
:mod:`family_assistant.a2a.attachments`. An attachment content part reaching
these functions raises, so a call site that skipped the transfer fails loudly
rather than putting an FA-internal identifier on the wire.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, cast

from family_assistant.a2a.types import (
    Artifact,
    FilePart,
    FileWithBytes,
    FileWithUri,
    Part,
    TextPart,
)

if TYPE_CHECKING:
    from family_assistant.llm.content_parts import (
        ContentPartDict,
        ImageUrlContentPartDict,
        TextContentPartDict,
    )


def text_to_a2a_part(text: str) -> Part:
    """Wrap plain text as an A2A part."""
    return Part(root=TextPart(text=text))


def convert_image_url_part(part: ImageUrlContentPartDict) -> Part:
    """Convert an image_url content part with no backing attachment.

    A ``data:`` URI carries its own bytes and MIME type, so it becomes an inline
    file part; any other URL is passed through as a reference.
    """
    url = part["image_url"].get("url", "")
    if url.startswith("data:"):
        comma_idx = url.find(",")
        if comma_idx == -1:
            return Part(root=FilePart(file=FileWithUri(uri=url, mime_type="image/*")))
        meta = url[5:comma_idx]
        mime_type = meta.split(";")[0] if ";" in meta else meta
        return Part(
            root=FilePart(
                file=FileWithBytes(bytes=url[comma_idx + 1 :], mime_type=mime_type)
            )
        )
    return Part(root=FilePart(file=FileWithUri(uri=url, mime_type="image/*")))


def text_content_parts_to_a2a_parts(parts: list[ContentPartDict]) -> list[Part]:
    """Convert content parts that need no attachment resolution.

    Raises:
        ValueError: If a part is an attachment reference (use
            :class:`~family_assistant.a2a.attachments.A2AAttachmentTransfer`)
            or an unrecognized type.
    """
    result: list[Part] = []
    for part in parts:
        part_type = part["type"]
        if part_type == "text":
            result.append(text_to_a2a_part(cast("TextContentPartDict", part)["text"]))
        elif part_type == "image_url":
            result.append(convert_image_url_part(cast("ImageUrlContentPartDict", part)))
        elif part_type == "attachment":
            raise ValueError(
                "Attachment content parts must be converted by "
                "A2AAttachmentTransfer, which can resolve their bytes"
            )
        else:
            raise ValueError(f"Unknown content part type: {part_type}")
    return result


def error_to_artifact(error_message: str) -> Artifact:
    """Create an artifact representing an error."""
    return Artifact(
        artifact_id=str(uuid.uuid4()),
        name="error",
        parts=[text_to_a2a_part(error_message)],
    )
