"""Bidirectional converters between A2A protocol types and FA internal types.

Handles conversion of:
- A2A Parts <-> ContentPartDict
- A2A Message <-> list[ContentPartDict]
- ChatInteractionResult -> A2A Artifact
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, cast

from family_assistant.a2a.types import (
    Artifact,
    DataPart,
    FilePart,
    FileWithBytes,
    FileWithUri,
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

    SDK Part is a RootModel wrapping TextPart/FilePart/DataPart in .root.

    Raises:
        ValueError: If a part cannot be converted (e.g. FilePart with no content).
    """
    result: list[ContentPartDict] = []
    for part in parts:
        inner = part.root
        if isinstance(inner, TextPart):
            result.append(text_content(inner.text))
        elif isinstance(inner, FilePart):
            file = inner.file
            if isinstance(file, FileWithUri):
                result.append(image_url_content(file.uri))
            elif isinstance(file, FileWithBytes):
                mime = file.mime_type or "application/octet-stream"
                result.append(image_url_content(f"data:{mime};base64,{file.bytes}"))
            else:
                raise ValueError("FilePart has neither URI nor bytes content")
        elif isinstance(inner, DataPart):
            result.append(text_content(json.dumps(inner.data)))
        else:
            raise ValueError(f"Unknown A2A part type: {type(inner).__name__}")
    return result


def _convert_text_part(part: TextContentPartDict) -> Part:
    return Part(root=TextPart(text=part["text"]))


def _convert_attachment_part(part: AttachmentContentPartDict) -> Part:
    return Part(
        root=FilePart(
            file=FileWithUri(
                uri=part["attachment_id"],
                mime_type="application/octet-stream",
            )
        )
    )


def _convert_image_url_part(part: ImageUrlContentPartDict) -> Part:
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
        parts.append(Part(root=TextPart(text=result.text_reply)))

    if result.attachment_ids:
        for att_id in result.attachment_ids:
            url = (attachment_urls or {}).get(att_id, att_id)
            parts.append(
                Part(
                    root=FilePart(
                        file=FileWithUri(uri=url, mime_type="application/octet-stream")
                    )
                )
            )

    if not parts:
        return None

    return Artifact(
        artifact_id=str(uuid.uuid4()),
        name="response",
        parts=parts,
    )


def error_to_artifact(error_message: str) -> Artifact:
    """Create an artifact representing an error."""
    return Artifact(
        artifact_id=str(uuid.uuid4()),
        name="error",
        parts=[Part(root=TextPart(text=error_message))],
    )
