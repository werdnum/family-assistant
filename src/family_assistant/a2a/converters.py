"""Bidirectional converters between A2A protocol types and FA internal types.

Handles conversion of:
- A2A Parts <-> ContentPartDict
- A2A Message <-> list[ContentPartDict]
- ChatInteractionResult -> A2A Artifact
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from family_assistant.a2a.types import (
    Artifact,
    DataPart,
    FileContent,
    FilePart,
    Message,
    Part,
    TextPart,
)
from family_assistant.llm.content_parts import (
    AttachmentContentPartDict,
    ContentPartDict,
    ImageUrlContentPartDict,
    TextContentPartDict,
    image_url_content,
    text_content,
)

if TYPE_CHECKING:
    from family_assistant.processing import ChatInteractionResult


def a2a_parts_to_content_parts(parts: list[Part]) -> list[ContentPartDict]:
    """Convert A2A message parts to FA ContentPartDict list.

    Raises:
        ValueError: If a part cannot be converted (e.g. FilePart with no content).
    """
    result: list[ContentPartDict] = []
    for part in parts:
        if isinstance(part, TextPart):
            result.append(text_content(part.text))
        elif isinstance(part, FilePart):
            if part.file.uri:
                result.append(image_url_content(part.file.uri))
            elif part.file.bytes:
                mime = part.file.mimeType or "application/octet-stream"
                result.append(
                    image_url_content(f"data:{mime};base64,{part.file.bytes}")
                )
            else:
                raise ValueError("FilePart has neither URI nor bytes content")
        elif isinstance(part, DataPart):
            result.append(text_content(json.dumps(part.data)))
        else:
            raise ValueError(f"Unknown A2A part type: {type(part).__name__}")
    return result


def _convert_text_part(part: TextContentPartDict) -> TextPart:
    return TextPart(text=part["text"])


def _convert_attachment_part(part: AttachmentContentPartDict) -> FilePart:
    return FilePart(
        file=FileContent(
            uri=part["attachment_id"],
            mimeType="application/octet-stream",
        )
    )


def _convert_image_url_part(part: ImageUrlContentPartDict) -> FilePart:
    url = part["image_url"].get("url", "")
    if url.startswith("data:"):
        comma_idx = url.find(",")
        if comma_idx == -1:
            return FilePart(file=FileContent(uri=url, mimeType="image/*"))
        meta = url[5:comma_idx]
        mime_type = meta.split(";")[0] if ";" in meta else meta
        return FilePart(
            file=FileContent(bytes=url[comma_idx + 1 :], mimeType=mime_type)
        )
    return FilePart(file=FileContent(uri=url, mimeType="image/*"))


def content_parts_to_a2a_parts(parts: list[ContentPartDict]) -> list[Part]:
    """Convert FA ContentPartDict list to A2A parts.

    Raises:
        ValueError: If a content part type is not recognized.
    """
    result: list[Part] = []
    for part in parts:
        part_type = part["type"]
        if part_type == "text":
            result.append(_convert_text_part(cast("TextContentPartDict", part)))
        elif part_type == "attachment":
            result.append(
                _convert_attachment_part(cast("AttachmentContentPartDict", part))
            )
        elif part_type == "image_url":
            result.append(
                _convert_image_url_part(cast("ImageUrlContentPartDict", part))
            )
        else:
            raise ValueError(f"Unknown content part type: {part_type}")
    return result


def a2a_message_to_content_parts(message: Message) -> list[ContentPartDict]:
    """Extract content parts from an A2A message."""
    return a2a_parts_to_content_parts(message.parts)


def chat_result_to_artifact(
    result: ChatInteractionResult,
    attachment_urls: dict[str, str] | None = None,
) -> Artifact | None:
    """Convert a ChatInteractionResult to an A2A Artifact.

    Args:
        result: The chat interaction result.
        attachment_urls: Optional mapping of attachment_id -> download URL.
    """
    parts: list[Part] = []

    if result.text_reply:
        parts.append(TextPart(text=result.text_reply))

    if result.attachment_ids:
        for att_id in result.attachment_ids:
            url = (attachment_urls or {}).get(att_id, att_id)
            parts.append(
                FilePart(file=FileContent(uri=url, mimeType="application/octet-stream"))
            )

    if not parts:
        return None

    return Artifact(
        name="response",
        parts=parts,
        index=0,
        lastChunk=True,
    )


def error_to_artifact(error_message: str) -> Artifact:
    """Create an artifact representing an error."""
    return Artifact(
        name="error",
        parts=[TextPart(text=error_message)],
        index=0,
        lastChunk=True,
    )
