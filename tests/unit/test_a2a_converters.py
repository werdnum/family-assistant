"""Unit tests for the pure (I/O-free) A2A protocol type converters.

Attachment-carrying conversions live in ``tests/unit/test_a2a_attachments.py``;
these functions deliberately refuse them.
"""

import pytest

from family_assistant.a2a.converters import (
    error_to_artifact,
    text_content_parts_to_a2a_parts,
    text_to_a2a_part,
)
from family_assistant.a2a.types import (
    FilePart,
    FileWithBytes,
    FileWithUri,
    TextPart,
)
from family_assistant.llm.content_parts import (
    ContentPartDict,
    attachment_content,
    image_url_content,
    text_content,
)


class TestTextContentPartsToA2AParts:
    def test_text_content(self) -> None:
        parts: list[ContentPartDict] = [text_content("hello")]
        result = text_content_parts_to_a2a_parts(parts)
        assert len(result) == 1
        assert isinstance(result[0].root, TextPart)
        assert result[0].root.text == "hello"

    def test_attachment_content_is_refused(self) -> None:
        """A bare attachment id must never reach the wire."""
        parts: list[ContentPartDict] = [attachment_content("att-123")]
        with pytest.raises(ValueError, match="A2AAttachmentTransfer"):
            text_content_parts_to_a2a_parts(parts)

    def test_unknown_content_part_type(self) -> None:
        parts: list[ContentPartDict] = [
            {"type": "file_placeholder", "file_reference": {}}  # type: ignore[list-item]
        ]
        with pytest.raises(ValueError, match="Unknown content part type"):
            text_content_parts_to_a2a_parts(parts)

    def test_image_url_content(self) -> None:
        parts: list[ContentPartDict] = [
            image_url_content("https://example.com/image.png")
        ]
        result = text_content_parts_to_a2a_parts(parts)
        assert len(result) == 1
        assert isinstance(result[0].root, FilePart)
        assert isinstance(result[0].root.file, FileWithUri)
        assert result[0].root.file.uri == "https://example.com/image.png"

    def test_image_url_data_uri(self) -> None:
        data_uri = "data:image/png;base64,iVBOR="
        parts: list[ContentPartDict] = [image_url_content(data_uri)]
        result = text_content_parts_to_a2a_parts(parts)
        assert len(result) == 1
        assert isinstance(result[0].root, FilePart)
        assert isinstance(result[0].root.file, FileWithBytes)
        assert result[0].root.file.mime_type == "image/png"
        assert result[0].root.file.bytes == "iVBOR="

    def test_image_url_data_uri_no_comma(self) -> None:
        """data: URI with no comma should be treated as a regular URL fallback."""
        data_uri = "data:image/png;base64"
        parts: list[ContentPartDict] = [image_url_content(data_uri)]
        result = text_content_parts_to_a2a_parts(parts)
        assert len(result) == 1
        assert isinstance(result[0].root, FilePart)
        assert isinstance(result[0].root.file, FileWithUri)
        assert result[0].root.file.uri == data_uri

    def test_image_url_data_uri_no_semicolon(self) -> None:
        """data: URI without semicolon (no ;base64 marker)."""
        data_uri = "data:text/plain,Hello%20World"
        parts: list[ContentPartDict] = [image_url_content(data_uri)]
        result = text_content_parts_to_a2a_parts(parts)
        assert len(result) == 1
        assert isinstance(result[0].root, FilePart)
        assert isinstance(result[0].root.file, FileWithBytes)
        assert result[0].root.file.mime_type == "text/plain"
        assert result[0].root.file.bytes == "Hello%20World"

    def test_empty_parts(self) -> None:
        assert text_content_parts_to_a2a_parts([]) == []


class TestTextToA2APart:
    def test_wraps_text(self) -> None:
        part = text_to_a2a_part("hi")
        assert isinstance(part.root, TextPart)
        assert part.root.text == "hi"


class TestErrorToArtifact:
    def test_creates_error_artifact(self) -> None:
        artifact = error_to_artifact("Something went wrong")
        assert artifact.name == "error"
        assert len(artifact.parts) == 1
        assert isinstance(artifact.parts[0].root, TextPart)
        assert artifact.parts[0].root.text == "Something went wrong"
        assert artifact.artifact_id
