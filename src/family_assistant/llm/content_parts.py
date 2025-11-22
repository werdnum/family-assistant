"""
Content part type definitions and helper functions.

This module contains TypedDict definitions and helper functions for content parts
used in LLM messages. It's separated from messages.py to avoid circular imports.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

# ===== Content Part Dicts (for API boundary) =====


class TextContentPartDict(TypedDict):
    """Dict representation of text content (used at API boundary)."""

    type: Literal["text"]
    text: str


class ImageUrlContentPartDict(TypedDict):
    """Dict representation of image content (used at API boundary)."""

    type: Literal["image_url"]
    # ast-grep-ignore: no-dict-any - Provider API compatibility (OpenAI/Google format)
    image_url: dict[str, Any]


class AttachmentContentPartDict(TypedDict):
    """Dict representation of attachment reference (used at API boundary)."""

    type: Literal["attachment"]
    attachment_id: str


class FileContentPartDict(TypedDict):
    """Dict representation of file placeholder (used at API boundary)."""

    type: Literal["file_placeholder"]
    # ast-grep-ignore: no-dict-any - Dynamic structure varies by provider/mock implementation
    file_reference: dict[str, Any]


# Union type for all content part dicts
ContentPartDict = (
    TextContentPartDict
    | ImageUrlContentPartDict
    | AttachmentContentPartDict
    | FileContentPartDict
)


# ===== Content Part Dict Helpers =====


def text_content(text: str) -> TextContentPartDict:
    """Create a text content part dict."""
    return {"type": "text", "text": text}


def image_url_content(url: str) -> ImageUrlContentPartDict:
    """Create an image URL content part dict."""
    return {"type": "image_url", "image_url": {"url": url}}


def attachment_content(attachment_id: str) -> AttachmentContentPartDict:
    """Create an attachment reference content part dict."""
    return {"type": "attachment", "attachment_id": attachment_id}
