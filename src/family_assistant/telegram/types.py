from dataclasses import dataclass
from typing import TypedDict


@dataclass
class AttachmentData:
    content: bytes
    filename: str
    mime_type: str
    description: str | None = None


class TriggerAttachment(TypedDict):
    """Attachment data structure for LLM trigger."""

    type: str
    content_url: str
    name: str | None
    size: int
    content_type: str
    attachment_id: str
