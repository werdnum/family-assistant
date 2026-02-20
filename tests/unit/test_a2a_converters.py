"""Unit tests for A2A protocol type converters."""

import pytest

from family_assistant.a2a.converters import (
    a2a_message_to_content_parts,
    a2a_parts_to_content_parts,
    chat_result_to_artifact,
    content_parts_to_a2a_parts,
    error_to_artifact,
)
from family_assistant.a2a.types import (
    DataPart,
    FileContent,
    FilePart,
    Message,
    Part,
    TextPart,
)
from family_assistant.llm.content_parts import (
    ContentPartDict,
    attachment_content,
    image_url_content,
    text_content,
)
from family_assistant.processing import ChatInteractionResult


class TestA2APartsToContentParts:
    def test_text_part(self) -> None:
        parts: list[Part] = [TextPart(text="hello world")]
        result = a2a_parts_to_content_parts(parts)
        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert result[0]["text"] == "hello world"

    def test_file_part_with_uri(self) -> None:
        parts: list[Part] = [
            FilePart(file=FileContent(uri="https://example.com/file.pdf"))
        ]
        result = a2a_parts_to_content_parts(parts)
        assert len(result) == 1
        assert result[0]["type"] == "attachment"
        assert result[0]["attachment_id"] == "https://example.com/file.pdf"

    def test_file_part_with_bytes(self) -> None:
        parts: list[Part] = [
            FilePart(file=FileContent(bytes="dGVzdA==", mimeType="text/plain"))
        ]
        result = a2a_parts_to_content_parts(parts)
        assert len(result) == 1
        assert result[0]["type"] == "attachment"
        assert "data:text/plain;base64,dGVzdA==" in result[0]["attachment_id"]

    def test_file_part_with_no_content_skipped(self) -> None:
        parts: list[Part] = [FilePart(file=FileContent())]
        result = a2a_parts_to_content_parts(parts)
        assert len(result) == 0

    def test_data_part_serialized_as_json(self) -> None:
        parts: list[Part] = [DataPart(data={"key": "value", "num": 42})]
        result = a2a_parts_to_content_parts(parts)
        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert '"key"' in result[0]["text"]
        assert '"value"' in result[0]["text"]

    def test_multiple_parts(self) -> None:
        parts: list[Part] = [
            TextPart(text="hello"),
            TextPart(text="world"),
            FilePart(file=FileContent(uri="file:///test")),
        ]
        result = a2a_parts_to_content_parts(parts)
        assert len(result) == 3

    def test_empty_parts(self) -> None:
        result = a2a_parts_to_content_parts([])
        assert result == []


class TestContentPartsToA2AParts:
    def test_text_content(self) -> None:
        parts: list[ContentPartDict] = [text_content("hello")]
        result = content_parts_to_a2a_parts(parts)
        assert len(result) == 1
        assert isinstance(result[0], TextPart)
        assert result[0].text == "hello"

    def test_attachment_content(self) -> None:
        parts: list[ContentPartDict] = [attachment_content("att-123")]
        result = content_parts_to_a2a_parts(parts)
        assert len(result) == 1
        assert isinstance(result[0], FilePart)
        assert result[0].file.uri == "att-123"

    def test_image_url_content(self) -> None:
        parts: list[ContentPartDict] = [
            image_url_content("https://example.com/image.png")
        ]
        result = content_parts_to_a2a_parts(parts)
        assert len(result) == 1
        assert isinstance(result[0], FilePart)
        assert result[0].file.uri == "https://example.com/image.png"

    def test_image_url_data_uri(self) -> None:
        data_uri = "data:image/png;base64,iVBOR="
        parts: list[ContentPartDict] = [image_url_content(data_uri)]
        result = content_parts_to_a2a_parts(parts)
        assert len(result) == 1
        assert isinstance(result[0], FilePart)
        assert result[0].file.mimeType == "image/png"
        assert result[0].file.bytes == "iVBOR="

    def test_empty_parts(self) -> None:
        result = content_parts_to_a2a_parts([])
        assert result == []


class TestA2AMessageToContentParts:
    def test_message_extracts_parts(self) -> None:
        msg = Message(role="user", parts=[TextPart(text="test message")])
        result = a2a_message_to_content_parts(msg)
        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert result[0]["text"] == "test message"


class TestChatResultToArtifact:
    def test_text_reply(self) -> None:
        result = ChatInteractionResult(text_reply="Hello from the assistant")
        artifact = chat_result_to_artifact(result)
        assert artifact is not None
        assert artifact.name == "response"
        assert len(artifact.parts) == 1
        assert isinstance(artifact.parts[0], TextPart)
        assert artifact.parts[0].text == "Hello from the assistant"
        assert artifact.lastChunk is True

    def test_with_attachments(self) -> None:
        result = ChatInteractionResult(
            text_reply="See attached", attachment_ids=["att-1", "att-2"]
        )
        artifact = chat_result_to_artifact(
            result, attachment_urls={"att-1": "/download/att-1"}
        )
        assert artifact is not None
        assert len(artifact.parts) == 3
        assert isinstance(artifact.parts[0], TextPart)
        assert isinstance(artifact.parts[1], FilePart)
        assert artifact.parts[1].file.uri == "/download/att-1"
        assert isinstance(artifact.parts[2], FilePart)
        assert artifact.parts[2].file.uri == "att-2"

    def test_empty_result_returns_none(self) -> None:
        result = ChatInteractionResult()
        artifact = chat_result_to_artifact(result)
        assert artifact is None

    def test_error_result(self) -> None:
        result = ChatInteractionResult(error_traceback="Something went wrong")
        assert result.has_error is True
        artifact = chat_result_to_artifact(result)
        assert artifact is None


class TestErrorToArtifact:
    def test_creates_error_artifact(self) -> None:
        artifact = error_to_artifact("Something went wrong")
        assert artifact.name == "error"
        assert len(artifact.parts) == 1
        assert isinstance(artifact.parts[0], TextPart)
        assert artifact.parts[0].text == "Something went wrong"
        assert artifact.lastChunk is True


class TestRoundTrip:
    """Test that content survives a round-trip through conversion."""

    def test_text_round_trip(self) -> None:
        original: list[ContentPartDict] = [text_content("round trip test")]
        a2a = content_parts_to_a2a_parts(original)
        back = a2a_parts_to_content_parts(a2a)
        assert len(back) == 1
        assert back[0]["type"] == "text"
        assert back[0]["text"] == "round trip test"

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/file.pdf",
            "file:///local/path",
            "/relative/path",
        ],
    )
    def test_attachment_round_trip(self, url: str) -> None:
        original: list[ContentPartDict] = [attachment_content(url)]
        a2a = content_parts_to_a2a_parts(original)
        back = a2a_parts_to_content_parts(a2a)
        assert len(back) == 1
        assert back[0]["type"] == "attachment"
        assert back[0]["attachment_id"] == url
