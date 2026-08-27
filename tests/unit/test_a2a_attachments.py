"""Unit tests for attachment transfer across the A2A boundary.

Covers both directions: an FA attachment becoming inline wire bytes, and a
peer's inline bytes becoming a registered FA attachment.
"""

from __future__ import annotations

import base64
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from family_assistant.a2a.attachments import (
    A2AAttachmentError,
    A2AAttachmentTransfer,
)
from family_assistant.a2a.result_converter import a2a_task_to_chat_result
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
    TaskState,
    TaskStatus,
    TextPart,
)
from family_assistant.llm.content_parts import (
    ContentPartDict,
    attachment_content,
    image_url_content,
    text_content,
)
from family_assistant.processing import ChatInteractionResult
from family_assistant.security.taint import (
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    artifact_taint_sources,
)
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncEngine

OWNER = "user_owner"
OTHER = "user_other"
PDF_BYTES = b"%PDF-1.4 fake document"


@pytest.fixture
async def transfer(
    db_engine: AsyncEngine,
) -> AsyncGenerator[tuple[A2AAttachmentTransfer, AttachmentRegistry, Database]]:
    with tempfile.TemporaryDirectory() as temp_dir:
        registry = AttachmentRegistry(
            storage_path=temp_dir, db_engine=db_engine, config=None
        )
        db_context = Database(engine=db_engine)
        yield A2AAttachmentTransfer(registry, db_context), registry, db_context


async def _store(
    registry: AttachmentRegistry,
    db_context: Database,
    *,
    owner_user_id: str | None = OWNER,
    filename: str = "report.pdf",
    content_type: str = "application/pdf",
    content: bytes = PDF_BYTES,
) -> str:
    metadata = await registry.store_and_register_tool_attachment(
        file_content=content,
        filename=filename,
        content_type=content_type,
        tool_name="test_tool",
        owner_user_id=owner_user_id,
        db_context=db_context,
    )
    return metadata.attachment_id


def _message(*parts: Part) -> Message:
    return Message(role=Role.user, parts=list(parts), message_id=str(uuid.uuid4()))


def _completed_task(*parts: Part) -> Task:
    return Task(
        id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.completed),
        artifacts=[Artifact(artifact_id="art-1", parts=list(parts))],
    )


class TestOutbound:
    @pytest.mark.asyncio
    async def test_attachment_is_sent_inline_with_type_and_name(
        self,
        transfer: tuple[A2AAttachmentTransfer, AttachmentRegistry, Database],
    ) -> None:
        codec, registry, db_context = transfer
        att_id = await _store(registry, db_context)

        parts = await codec.to_a2a_parts(
            [text_content("here it is"), attachment_content(att_id)],
            acting_user_id=OWNER,
        )

        assert isinstance(parts[0].root, TextPart)
        file_part = parts[1].root
        assert isinstance(file_part, FilePart)
        assert isinstance(file_part.file, FileWithBytes)
        assert base64.b64decode(file_part.file.bytes) == PDF_BYTES
        assert file_part.file.mime_type == "application/pdf"
        assert file_part.file.name == "report.pdf"

    @pytest.mark.asyncio
    async def test_image_url_resolved_from_attachment_uses_real_metadata(
        self,
        transfer: tuple[A2AAttachmentTransfer, AttachmentRegistry, Database],
    ) -> None:
        codec, registry, db_context = transfer
        att_id = await _store(
            registry,
            db_context,
            filename="photo.png",
            content_type="image/png",
            content=b"\x89PNG fake",
        )
        part: ContentPartDict = image_url_content(
            "data:image/png;base64,ignored", attachment_id=att_id
        )

        parts = await codec.to_a2a_parts([part], acting_user_id=OWNER)

        file_part = parts[0].root
        assert isinstance(file_part, FilePart)
        assert isinstance(file_part.file, FileWithBytes)
        assert base64.b64decode(file_part.file.bytes) == b"\x89PNG fake"
        assert file_part.file.name == "photo.png"

    @pytest.mark.asyncio
    async def test_plain_image_url_passes_through(
        self,
        transfer: tuple[A2AAttachmentTransfer, AttachmentRegistry, Database],
    ) -> None:
        codec, _registry, _db = transfer
        parts = await codec.to_a2a_parts(
            [image_url_content("https://example.com/i.png")], acting_user_id=OWNER
        )
        file_part = parts[0].root
        assert isinstance(file_part, FilePart)
        assert isinstance(file_part.file, FileWithUri)
        assert file_part.file.uri == "https://example.com/i.png"

    @pytest.mark.asyncio
    async def test_attachment_owned_by_someone_else_is_an_error(
        self,
        transfer: tuple[A2AAttachmentTransfer, AttachmentRegistry, Database],
    ) -> None:
        codec, registry, db_context = transfer
        att_id = await _store(registry, db_context, owner_user_id=OWNER)

        with pytest.raises(A2AAttachmentError, match="not available"):
            await codec.to_a2a_parts([attachment_content(att_id)], acting_user_id=OTHER)


class TestResultArtifact:
    @pytest.mark.asyncio
    async def test_result_attachments_are_inlined(
        self,
        transfer: tuple[A2AAttachmentTransfer, AttachmentRegistry, Database],
    ) -> None:
        codec, registry, db_context = transfer
        att_id = await _store(registry, db_context)
        result = ChatInteractionResult.success(
            text_reply="See attached", attachment_ids=[att_id]
        )

        artifact = await codec.result_to_artifact(result, acting_user_id=OWNER)

        assert artifact is not None
        assert artifact.name == "response"
        file_part = artifact.parts[1].root
        assert isinstance(file_part, FilePart)
        assert isinstance(file_part.file, FileWithBytes)
        assert base64.b64decode(file_part.file.bytes) == PDF_BYTES
        assert file_part.file.name == "report.pdf"

    @pytest.mark.asyncio
    async def test_unreadable_attachment_is_an_error(
        self,
        transfer: tuple[A2AAttachmentTransfer, AttachmentRegistry, Database],
    ) -> None:
        """A dangling URL the peer would be refused is worse than a failure."""
        codec, registry, db_context = transfer
        att_id = await _store(registry, db_context, owner_user_id=OWNER)
        result = ChatInteractionResult.success(
            text_reply="See attached", attachment_ids=[att_id]
        )

        with pytest.raises(A2AAttachmentError, match="not available"):
            await codec.result_to_artifact(result, acting_user_id=OTHER)

    @pytest.mark.asyncio
    async def test_oversized_attachment_is_an_error(
        self,
        transfer: tuple[A2AAttachmentTransfer, AttachmentRegistry, Database],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Inline bytes are the only transfer, so the ceiling is reported."""
        codec, registry, db_context = transfer
        monkeypatch.setattr(
            "family_assistant.a2a.attachments.MAX_INLINE_ATTACHMENT_BYTES", 8
        )
        att_id = await _store(
            registry,
            db_context,
            filename="huge.bin",
            content_type="application/octet-stream",
            content=b"more than eight bytes",
        )
        result = ChatInteractionResult.success(
            text_reply="See attached", attachment_ids=[att_id]
        )

        with pytest.raises(A2AAttachmentError, match="exceeds the inline transfer"):
            await codec.result_to_artifact(result, acting_user_id=OWNER)

    @pytest.mark.asyncio
    async def test_error_result_has_no_artifact(
        self,
        transfer: tuple[A2AAttachmentTransfer, AttachmentRegistry, Database],
    ) -> None:
        codec, _registry, _db = transfer
        result = ChatInteractionResult.error(
            text_reply="broke", error_traceback="broke"
        )
        assert await codec.result_to_artifact(result, acting_user_id=OWNER) is None

    @pytest.mark.asyncio
    async def test_empty_result_has_no_artifact(
        self,
        transfer: tuple[A2AAttachmentTransfer, AttachmentRegistry, Database],
    ) -> None:
        codec, _registry, _db = transfer
        artifact = await codec.result_to_artifact(
            ChatInteractionResult.success(), acting_user_id=OWNER
        )
        assert artifact is None


class TestInboundMessage:
    @pytest.mark.asyncio
    async def test_inline_file_becomes_an_attachment(
        self,
        transfer: tuple[A2AAttachmentTransfer, AttachmentRegistry, Database],
    ) -> None:
        codec, registry, db_context = transfer
        message = _message(
            Part(root=TextPart(text="summarize this")),
            Part(
                root=FilePart(
                    file=FileWithBytes(
                        bytes=base64.b64encode(PDF_BYTES).decode(),
                        mime_type="application/pdf",
                        name="inbound.pdf",
                    )
                )
            ),
        )

        parts = await codec.message_to_content_parts(
            message, conversation_id="a2a-ctx", owner_user_id=OWNER
        )

        assert parts[0]["type"] == "text"
        assert parts[1]["type"] == "attachment"
        att_id = parts[1]["attachment_id"]
        metadata = await registry.get_attachment(
            db_context, att_id, acting_user_id=OWNER
        )
        assert metadata is not None
        assert metadata.mime_type == "application/pdf"
        assert metadata.conversation_id == "a2a-ctx"
        assert metadata.metadata["original_filename"] == "inbound.pdf"
        content = await registry.get_attachment_content(
            db_context, att_id, acting_user_id=OWNER
        )
        assert content == PDF_BYTES

    @pytest.mark.asyncio
    async def test_data_uri_file_becomes_an_attachment(
        self,
        transfer: tuple[A2AAttachmentTransfer, AttachmentRegistry, Database],
    ) -> None:
        codec, registry, db_context = transfer
        encoded = base64.b64encode(b"hello").decode()
        message = _message(
            Part(
                root=FilePart(file=FileWithUri(uri=f"data:text/plain;base64,{encoded}"))
            )
        )

        parts = await codec.message_to_content_parts(
            message, conversation_id=None, owner_user_id=None
        )

        assert parts[0]["type"] == "attachment"
        content = await registry.get_attachment_content(
            db_context, parts[0]["attachment_id"], acting_user_id=None
        )
        assert content == b"hello"

    @pytest.mark.asyncio
    async def test_stored_file_carries_the_message_taint(
        self,
        transfer: tuple[A2AAttachmentTransfer, AttachmentRegistry, Database],
    ) -> None:
        """A peer's file must not read as untrusted-by-omission on a later turn."""
        codec, registry, db_context = transfer
        source = TaintSource(
            source_type=TaintSourceType.EMAIL,
            source_id="msg-1",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset({"source_unknown_external"}),
            reason="forwarded email",
        )
        message = _message(
            Part(
                root=FilePart(file=FileWithBytes(bytes=base64.b64encode(b"x").decode()))
            )
        )

        parts = await codec.message_to_content_parts(
            message,
            conversation_id=None,
            owner_user_id=None,
            taint_sources=[source],
        )

        part = parts[0]
        assert part["type"] == "attachment"
        metadata = await registry.get_attachment(
            db_context, part["attachment_id"], acting_user_id=None
        )
        assert metadata is not None
        assert artifact_taint_sources(metadata.metadata, source_id="att") == (source,)

    @pytest.mark.asyncio
    async def test_remote_uri_file_stays_a_reference(
        self,
        transfer: tuple[A2AAttachmentTransfer, AttachmentRegistry, Database],
    ) -> None:
        codec, _registry, _db = transfer
        message = _message(
            Part(root=FilePart(file=FileWithUri(uri="https://example.com/f.pdf")))
        )

        parts = await codec.message_to_content_parts(
            message, conversation_id=None, owner_user_id=None
        )

        assert parts[0]["type"] == "image_url"
        assert parts[0]["image_url"]["url"] == "https://example.com/f.pdf"

    @pytest.mark.asyncio
    async def test_a_bad_part_stores_none_of_the_message(
        self,
        transfer: tuple[A2AAttachmentTransfer, AttachmentRegistry, Database],
    ) -> None:
        """One malformed file must not leave its predecessors registered."""
        codec, registry, db_context = transfer
        message = _message(
            Part(
                root=FilePart(
                    file=FileWithBytes(
                        bytes=base64.b64encode(b"good one").decode(),
                        mime_type="text/plain",
                        name="good.txt",
                    )
                )
            ),
            Part(root=FilePart(file=FileWithBytes(bytes="not base64!!"))),
        )

        with pytest.raises(ValueError, match="not valid base64"):
            await codec.message_to_content_parts(
                message, conversation_id="a2a-partial", owner_user_id=OWNER
            )

        stored = await registry.get_recent_attachments_for_conversation(
            db_context,
            "a2a-partial",
            datetime.now(UTC) - timedelta(minutes=5),
            acting_user_id=OWNER,
        )
        assert stored == []

    @pytest.mark.asyncio
    async def test_a_storage_failure_removes_what_it_already_stored(
        self,
        transfer: tuple[A2AAttachmentTransfer, AttachmentRegistry, Database],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Nothing collects a tool-source orphan, so the batch cleans up after itself."""
        codec, registry, db_context = transfer
        real_store = registry.store_and_register_tool_attachment
        calls = {"n": 0}

        async def _fail_on_second(*args: object, **kwargs: object) -> object:
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk on fire")
            return await real_store(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(
            registry, "store_and_register_tool_attachment", _fail_on_second
        )
        message = _message(*[
            Part(
                root=FilePart(file=FileWithBytes(bytes=base64.b64encode(body).decode()))
            )
            for body in (b"first", b"second")
        ])

        with pytest.raises(OSError, match="disk on fire"):
            await codec.message_to_content_parts(
                message, conversation_id="a2a-storage-fail", owner_user_id=OWNER
            )

        stored = await registry.get_recent_attachments_for_conversation(
            db_context,
            "a2a-storage-fail",
            datetime.now(UTC) - timedelta(minutes=5),
            acting_user_id=OWNER,
        )
        assert stored == []

    @pytest.mark.asyncio
    async def test_data_part_becomes_json_text(
        self,
        transfer: tuple[A2AAttachmentTransfer, AttachmentRegistry, Database],
    ) -> None:
        codec, _registry, _db = transfer
        message = _message(Part(root=DataPart(data={"key": "value"})))
        parts = await codec.message_to_content_parts(
            message, conversation_id=None, owner_user_id=None
        )
        assert parts[0]["type"] == "text"
        assert '"key"' in parts[0]["text"]

    @pytest.mark.asyncio
    async def test_invalid_base64_is_rejected(
        self,
        transfer: tuple[A2AAttachmentTransfer, AttachmentRegistry, Database],
    ) -> None:
        codec, _registry, _db = transfer
        message = _message(
            Part(root=FilePart(file=FileWithBytes(bytes="not base64!!")))
        )
        with pytest.raises(ValueError, match="not valid base64"):
            await codec.message_to_content_parts(
                message, conversation_id=None, owner_user_id=None
            )


class TestInboundSizeLimits:
    @pytest.mark.asyncio
    async def test_media_over_the_multimodal_limit_is_refused(
        self,
        transfer: tuple[A2AAttachmentTransfer, AttachmentRegistry, Database],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A peer's image goes straight into a turn, so it gets the media limit."""
        codec, registry, _db = transfer
        monkeypatch.setattr(registry, "max_multimodal_size", 8)
        message = _message(
            Part(
                root=FilePart(
                    file=FileWithBytes(
                        bytes=base64.b64encode(b"more than eight bytes").decode(),
                        mime_type="image/png",
                        name="big.png",
                    )
                )
            )
        )

        with pytest.raises(A2AAttachmentError, match="exceeds the image/png limit"):
            await codec.message_to_content_parts(
                message, conversation_id=None, owner_user_id=None
            )


class TestTaskResultFiles:
    @pytest.mark.asyncio
    async def test_task_files_become_result_attachments(
        self,
        transfer: tuple[A2AAttachmentTransfer, AttachmentRegistry, Database],
    ) -> None:
        codec, registry, db_context = transfer
        task = _completed_task(
            Part(root=TextPart(text="Here is your chart")),
            Part(
                root=FilePart(
                    file=FileWithBytes(
                        bytes=base64.b64encode(b"chart-bytes").decode(),
                        mime_type="image/png",
                        name="chart.png",
                    )
                )
            ),
        )

        result = await a2a_task_to_chat_result(
            task, attachments=codec, conversation_id="conv-1", owner_user_id=OWNER
        )

        assert result.has_error is False
        assert result.text_reply == "Here is your chart"
        assert result.attachment_ids is not None
        content = await registry.get_attachment_content(
            db_context, result.attachment_ids[0], acting_user_id=OWNER
        )
        assert content == b"chart-bytes"

    @pytest.mark.asyncio
    async def test_returned_file_keeps_the_turn_taint(
        self,
        transfer: tuple[A2AAttachmentTransfer, AttachmentRegistry, Database],
    ) -> None:
        """A file coming back from an untrusted request must not be downgraded."""
        codec, registry, db_context = transfer
        turn_source = TaintSource(
            source_type=TaintSourceType.EMAIL,
            source_id="msg-9",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset({"source_unknown_external"}),
            reason="forwarded email",
        )
        task = _completed_task(
            Part(
                root=FilePart(file=FileWithBytes(bytes=base64.b64encode(b"z").decode()))
            )
        )

        result = await a2a_task_to_chat_result(
            task, attachments=codec, turn_taint_sources=[turn_source]
        )

        assert result.attachment_ids is not None
        metadata = await registry.get_attachment(
            db_context, result.attachment_ids[0], acting_user_id=None
        )
        assert metadata is not None
        assert turn_source in artifact_taint_sources(metadata.metadata, source_id="a")

    @pytest.mark.asyncio
    async def test_polled_result_file_gets_the_conservative_tier(
        self,
        transfer: tuple[A2AAttachmentTransfer, AttachmentRegistry, Database],
    ) -> None:
        """The polled path knows nothing of the turn, so it must not assume trust."""
        codec, registry, db_context = transfer
        task = _completed_task(
            Part(
                root=FilePart(file=FileWithBytes(bytes=base64.b64encode(b"z").decode()))
            )
        )

        result = await a2a_task_to_chat_result(task, attachments=codec)

        assert result.attachment_ids is not None
        metadata = await registry.get_attachment(
            db_context, result.attachment_ids[0], acting_user_id=None
        )
        assert metadata is not None
        tiers = {
            source.tier
            for source in artifact_taint_sources(metadata.metadata, source_id="a")
        }
        assert tiers == {SourceTrustTier.UNKNOWN_EXTERNAL}

    @pytest.mark.asyncio
    async def test_file_only_task_is_not_an_error(
        self,
        transfer: tuple[A2AAttachmentTransfer, AttachmentRegistry, Database],
    ) -> None:
        codec, _registry, _db = transfer
        task = _completed_task(
            Part(
                root=FilePart(file=FileWithBytes(bytes=base64.b64encode(b"x").decode()))
            )
        )

        result = await a2a_task_to_chat_result(task, attachments=codec)

        assert result.has_error is False
        assert result.attachment_ids is not None
        assert len(result.attachment_ids) == 1

    @pytest.mark.asyncio
    async def test_remote_uri_file_is_described_not_stored(
        self,
        transfer: tuple[A2AAttachmentTransfer, AttachmentRegistry, Database],
    ) -> None:
        codec, _registry, _db = transfer
        task = _completed_task(
            Part(root=FilePart(file=FileWithUri(uri="https://example.com/f.pdf")))
        )

        result = await a2a_task_to_chat_result(task, attachments=codec)

        assert result.attachment_ids is None
        assert "https://example.com/f.pdf" in result.text_reply

    @pytest.mark.asyncio
    async def test_files_in_a_bare_agent_message_are_stored(
        self,
        transfer: tuple[A2AAttachmentTransfer, AttachmentRegistry, Database],
    ) -> None:
        """An agent that answers with a message rather than artifacts."""
        codec, _registry, _db = transfer
        message = Message(
            role=Role.agent,
            parts=[
                Part(root=TextPart(text="done")),
                Part(
                    root=FilePart(
                        file=FileWithBytes(bytes=base64.b64encode(b"y").decode())
                    )
                ),
            ],
            message_id="m1",
        )
        task = Task(
            id="t",
            context_id="c",
            status=TaskStatus(state=TaskState.completed, message=message),
            history=[message],
        )

        result = await a2a_task_to_chat_result(task, attachments=codec)

        assert result.attachment_ids is not None
        assert len(result.attachment_ids) == 1
        assert result.text_reply == "done"
