"""Attachment transfer across the A2A boundary.

The single chokepoint through which attachment bytes enter or leave the A2A
protocol layer. Outbound, an FA attachment id is resolved to its bytes, MIME
type and filename and sent inline; inbound, a peer's inline bytes are
registered with the :class:`AttachmentRegistry` and referenced by attachment id.

The pure converters in :mod:`family_assistant.a2a.converters` deliberately
refuse attachment content parts, so a call site that bypasses this class fails
loudly instead of putting an FA-internal identifier on the wire.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import mimetypes
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from family_assistant.a2a.converters import (
    convert_image_url_part,
    text_to_a2a_part,
)
from family_assistant.a2a.types import (
    Artifact,
    DataPart,
    FilePart,
    FileWithBytes,
    FileWithUri,
    Message,
    Part,
    Role,
    Task,
    TextPart,
)
from family_assistant.llm.content_parts import (
    AttachmentContentPartDict,
    ContentPartDict,
    ImageUrlContentPartDict,
    TextContentPartDict,
    attachment_content,
    image_url_content,
    text_content,
)

if TYPE_CHECKING:
    from family_assistant.processing.types import ChatInteractionResult
    from family_assistant.services.attachment_registry import (
        AttachmentMetadata,
        AttachmentRegistry,
    )
    from family_assistant.storage.database import Database

logger = logging.getLogger(__name__)

# Limit on base64-encoded size in the JSON-RPC payload (not decoded file size).
# Base64 inflates by ~33%, so this allows ~7.5 MB raw files.
MAX_INLINE_ATTACHMENT_BYTES = 10 * 1024 * 1024

DEFAULT_MIME_TYPE = "application/octet-stream"

# Attachments registered from A2A traffic are recorded under this source id, so
# the registry (and anything auditing it) can tell where the bytes came from.
A2A_ATTACHMENT_SOURCE = "a2a"


class A2AAttachmentError(ValueError):
    """An attachment could not be moved across the A2A boundary.

    A ``ValueError`` so the JSON-RPC layer reports it as an invalid-params
    error, alongside the other conversion failures.
    """


@dataclass(frozen=True)
class InlineFile:
    """A file's bytes with the metadata needed to store or send it."""

    content: bytes
    mime_type: str
    filename: str


class A2AAttachmentTransfer:
    """Moves attachment bytes between FA storage and A2A parts."""

    def __init__(
        self,
        attachment_registry: AttachmentRegistry,
        db_context: Database,
    ) -> None:
        self._registry = attachment_registry
        self._db = db_context

    # ===== FA -> A2A =====

    async def to_a2a_parts(
        self,
        content_parts: list[ContentPartDict],
        *,
        acting_user_id: str | None,
    ) -> list[Part]:
        """Convert FA content parts to A2A parts, inlining attachment bytes.

        Raises:
            A2AAttachmentError: If a referenced attachment cannot be read.
            ValueError: If a content part type is not recognized.
        """
        result: list[Part] = []
        for part in content_parts:
            part_type = part["type"]
            if part_type == "text":
                text_part = cast("TextContentPartDict", part)
                result.append(text_to_a2a_part(text_part["text"]))
            elif part_type == "attachment":
                attachment_part = cast("AttachmentContentPartDict", part)
                result.append(
                    await self._attachment_to_file_part(
                        attachment_part["attachment_id"],
                        acting_user_id=acting_user_id,
                    )
                )
            elif part_type == "image_url":
                image_part = cast("ImageUrlContentPartDict", part)
                result.append(
                    await self._image_url_to_part(
                        image_part, acting_user_id=acting_user_id
                    )
                )
            else:
                raise ValueError(f"Unknown content part type: {part_type}")
        return result

    async def _image_url_to_part(
        self,
        part: ImageUrlContentPartDict,
        *,
        acting_user_id: str | None,
    ) -> Part:
        """Convert an image_url part, preferring the attachment it came from.

        A part resolved from an attachment carries its id; going back to the
        registry recovers the real MIME type and filename, which the data URI
        alone does not carry.
        """
        attachment_id = part.get("attachment_id")
        if attachment_id:
            return await self._attachment_to_file_part(
                attachment_id, acting_user_id=acting_user_id
            )
        return convert_image_url_part(part)

    async def _attachment_to_file_part(
        self, attachment_id: str, *, acting_user_id: str | None
    ) -> Part:
        metadata = await self._registry.get_attachment(
            self._db, attachment_id, acting_user_id=acting_user_id
        )
        if metadata is None:
            raise A2AAttachmentError(
                f"Attachment {attachment_id} is not available to this user"
            )
        content = await self._registry.get_attachment_content(
            self._db, attachment_id, acting_user_id=acting_user_id
        )
        if content is None:
            raise A2AAttachmentError(
                f"Attachment {attachment_id} has no stored content to send"
            )
        return _file_part(
            InlineFile(
                content=content,
                mime_type=metadata.mime_type or DEFAULT_MIME_TYPE,
                filename=attachment_filename(metadata),
            )
        )

    async def result_to_artifact(
        self,
        result: ChatInteractionResult,
        *,
        acting_user_id: str | None,
    ) -> Artifact | None:
        """Convert a ChatInteractionResult to an A2A Artifact.

        Raises:
            A2AAttachmentError: If a response attachment cannot be sent.
        """
        if result.has_error:
            return None

        parts: list[Part] = []
        if result.text_reply:
            parts.append(text_to_a2a_part(result.text_reply))

        parts.extend(
            await self.response_attachment_parts(
                result.attachment_ids, acting_user_id=acting_user_id
            )
        )

        if not parts:
            return None

        return Artifact(
            artifact_id=str(uuid.uuid4()),
            name="response",
            parts=parts,
        )

    async def response_attachment_parts(
        self,
        attachment_ids: list[str] | None,
        *,
        acting_user_id: str | None,
    ) -> list[Part]:
        """File parts for the attachments a turn queued for its response.

        Raises:
            A2AAttachmentError: If an attachment cannot be sent — unreadable,
                or too large to inline. Inline bytes are the only transfer this
                boundary has: FA's own download URL needs an FA credential the
                peer does not hold, and a peer's URI is not fetched either, so
                offering one would be a dangling reference on a task reported
                completed. A signed, peer-usable transfer URL is what would
                lift the size ceiling; until there is one, the ceiling is
                reported rather than papered over.
        """
        return [
            await self._result_attachment_part(
                attachment_id, acting_user_id=acting_user_id
            )
            for attachment_id in attachment_ids or []
        ]

    async def _result_attachment_part(
        self, attachment_id: str, *, acting_user_id: str | None
    ) -> Part:
        metadata = await self._registry.get_attachment(
            self._db, attachment_id, acting_user_id=acting_user_id
        )
        if metadata is None:
            raise A2AAttachmentError(
                f"Response attachment {attachment_id} is not available to this user"
            )
        content = await self._registry.get_attachment_content(
            self._db, attachment_id, acting_user_id=acting_user_id
        )
        if content is None:
            raise A2AAttachmentError(
                f"Response attachment {attachment_id} has no stored content to send"
            )
        if _encoded_size(len(content)) > MAX_INLINE_ATTACHMENT_BYTES:
            raise A2AAttachmentError(
                f"Response attachment {attachment_id} ({len(content)} bytes) exceeds "
                f"the inline transfer limit ({MAX_INLINE_ATTACHMENT_BYTES} bytes "
                f"encoded)"
            )
        return _file_part(
            InlineFile(
                content=content,
                mime_type=metadata.mime_type or DEFAULT_MIME_TYPE,
                filename=attachment_filename(metadata),
            )
        )

    # ===== A2A -> FA =====

    async def message_to_content_parts(
        self,
        message: Message,
        *,
        conversation_id: str | None,
        owner_user_id: str | None,
    ) -> list[ContentPartDict]:
        """Convert an inbound A2A message to FA content parts.

        Inline file bytes are registered as attachments and referenced by id; a
        file given only as a remote URI stays a reference (see the design doc's
        deliberate simplifications).

        Raises:
            ValueError: If a part cannot be converted.
        """
        result: list[ContentPartDict] = []
        for part in message.parts:
            inner = part.root
            if isinstance(inner, TextPart):
                result.append(text_content(inner.text))
            elif isinstance(inner, DataPart):
                result.append(text_content(_data_part_text(inner)))
            elif isinstance(inner, FilePart):
                result.append(
                    await self._file_part_to_content_part(
                        inner,
                        conversation_id=conversation_id,
                        owner_user_id=owner_user_id,
                    )
                )
            else:
                raise ValueError(f"Unknown A2A part type: {type(inner).__name__}")
        return result

    async def _file_part_to_content_part(
        self,
        file_part: FilePart,
        *,
        conversation_id: str | None,
        owner_user_id: str | None,
    ) -> ContentPartDict:
        inline = _inline_file(file_part)
        if inline is None:
            uri = (
                file_part.file.uri if isinstance(file_part.file, FileWithUri) else None
            )
            if uri is None:
                raise ValueError("FilePart has neither URI nor bytes content")
            return image_url_content(uri)
        attachment_id = await self._store(
            inline, conversation_id=conversation_id, owner_user_id=owner_user_id
        )
        return attachment_content(attachment_id)

    async def store_task_files(
        self,
        task: Task,
        *,
        conversation_id: str | None,
        owner_user_id: str | None,
    ) -> list[str]:
        """Register every inline file a task carries as an attachment.

        Walks the task's artifacts, falling back to the terminal agent message
        when there are none — the same precedence the text extraction uses, so
        an agent that answers with a bare message is not silently stripped of
        its files.

        Returns the attachment ids, in the order the files appear.
        """
        attachment_ids: list[str] = []
        for parts in _file_carrying_parts(task):
            for part in parts:
                inner = part.root
                if not isinstance(inner, FilePart):
                    continue
                inline = _inline_file(inner)
                if inline is None:
                    continue
                attachment_ids.append(
                    await self._store(
                        inline,
                        conversation_id=conversation_id,
                        owner_user_id=owner_user_id,
                    )
                )
        return attachment_ids

    async def _store(
        self,
        inline: InlineFile,
        *,
        conversation_id: str | None,
        owner_user_id: str | None,
    ) -> str:
        metadata = await self._registry.store_and_register_tool_attachment(
            file_content=inline.content,
            filename=inline.filename,
            content_type=inline.mime_type,
            tool_name=A2A_ATTACHMENT_SOURCE,
            description=f"File received over A2A: {inline.filename}",
            conversation_id=conversation_id,
            owner_user_id=owner_user_id,
            db_context=self._db,
        )
        logger.info(
            "Registered A2A file '%s' (%s, %d bytes) as attachment %s",
            inline.filename,
            inline.mime_type,
            len(inline.content),
            metadata.attachment_id,
        )
        return metadata.attachment_id


def _file_carrying_parts(task: Task) -> list[list[Part]]:
    """Part lists to search for files, in the same precedence as text."""
    if task.artifacts:
        return [artifact.parts for artifact in task.artifacts]
    for message in reversed(task.history or []):
        if message.role == Role.agent:
            return [message.parts]
    return []


def attachment_filename(metadata: AttachmentMetadata) -> str:
    """The best filename available for an attachment."""
    original = metadata.metadata.get("original_filename")
    if isinstance(original, str) and original:
        return original
    extension = mimetypes.guess_extension(metadata.mime_type or "") or ""
    return f"{metadata.attachment_id}{extension}"


def _file_part(inline: InlineFile) -> Part:
    return Part(
        root=FilePart(
            file=FileWithBytes(
                bytes=base64.b64encode(inline.content).decode("ascii"),
                mime_type=inline.mime_type,
                name=inline.filename,
            )
        )
    )


def _data_part_text(part: DataPart) -> str:
    return json.dumps(part.data)


def _encoded_size(raw_size: int) -> int:
    """Base64-encoded size of ``raw_size`` bytes."""
    return ((raw_size + 2) // 3) * 4


def _inline_file(file_part: FilePart) -> InlineFile | None:
    """Extract inline bytes from a FilePart, or None if it is URI-only."""
    file = file_part.file
    if isinstance(file, FileWithBytes):
        return InlineFile(
            content=_decode_base64(file.bytes),
            mime_type=file.mime_type or DEFAULT_MIME_TYPE,
            filename=file.name or _generated_filename(file.mime_type),
        )
    if isinstance(file, FileWithUri) and file.uri.startswith("data:"):
        return _inline_from_data_uri(file.uri, file.name)
    return None


def _inline_from_data_uri(uri: str, name: str | None) -> InlineFile:
    comma_idx = uri.find(",")
    if comma_idx == -1:
        raise ValueError("Malformed data: URI in FilePart (no comma)")
    meta = uri[5:comma_idx]
    if not meta.endswith(";base64"):
        raise ValueError("Only base64-encoded data: URIs are supported in FileParts")
    mime_type = meta.removesuffix(";base64") or DEFAULT_MIME_TYPE
    return InlineFile(
        content=_decode_base64(uri[comma_idx + 1 :]),
        mime_type=mime_type,
        filename=name or _generated_filename(mime_type),
    )


def _decode_base64(encoded: str) -> bytes:
    if len(encoded) > MAX_INLINE_ATTACHMENT_BYTES:
        raise A2AAttachmentError(
            f"Inline file size ({len(encoded)} bytes encoded) exceeds "
            f"limit ({MAX_INLINE_ATTACHMENT_BYTES} bytes)"
        )
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"FilePart bytes are not valid base64: {exc}") from exc


def _generated_filename(mime_type: str | None) -> str:
    extension = mimetypes.guess_extension(mime_type or "") or ""
    return f"a2a-file-{uuid.uuid4().hex[:8]}{extension}"
