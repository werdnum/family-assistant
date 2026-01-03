from dataclasses import dataclass


@dataclass
class AttachmentData:
    content: bytes
    filename: str
    mime_type: str
    description: str | None = None
