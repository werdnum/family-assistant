"""Attachments carry provenance from registration, and note reads honour it.

Milestone 3 of docs/design/ambient-note-admission-at-write-time.md.
"""

from __future__ import annotations

import tempfile
from typing import TYPE_CHECKING, cast

import pytest

from family_assistant.security.note_provenance import NoteProvenanceStamp
from family_assistant.security.taint import SourceTrustTier, TurnTaintState
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.notes import NoteWritePolicy
from family_assistant.tools.infrastructure import (
    LocalToolsProvider,
    TaintTrackingToolsProvider,
)
from family_assistant.tools.metadata import (
    ToolRegistration,
    ToolTag,
    make_local_tool_metadata,
)
from family_assistant.tools.notes import get_note_tool
from family_assistant.tools.taint_helpers import tool_attachment_taint_state
from family_assistant.tools.types import ToolResult
from tests.functional.notes.ambient_helpers import (
    state_at,
    tool_context,
    tracker_at,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.tools.types import ToolDefinition, ToolExecutionContext


def _registry(db_engine: AsyncEngine) -> AttachmentRegistry:
    return AttachmentRegistry(
        storage_path=tempfile.mkdtemp(), db_engine=db_engine, config=None
    )


def _stamped_tier(metadata: dict[str, object]) -> SourceTrustTier:
    return TurnTaintState.from_metadata(metadata.get("taint_metadata")).max_tier


async def _store_output(exec_context: ToolExecutionContext) -> ToolResult:
    """A tool that stores an attachment while it is still executing."""
    registry = exec_context.attachment_registry
    assert registry is not None
    metadata = await registry.store_and_register_tool_attachment(
        file_content=b"rendered",
        filename="out.txt",
        content_type="text/plain",
        tool_name="store_output",
        db_context=exec_context.db_context,
        taint_state=tool_attachment_taint_state(exec_context),
    )
    return ToolResult(text=metadata.attachment_id)


def _provider(output_tag: ToolTag) -> TaintTrackingToolsProvider:
    return TaintTrackingToolsProvider(
        LocalToolsProvider(
            registrations=[
                ToolRegistration(
                    definition=cast(
                        "ToolDefinition",
                        {
                            "type": "function",
                            "function": {
                                "name": "store_output",
                                "description": "Store an output.",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        },
                    ),
                    implementation=_store_output,
                    metadata=make_local_tool_metadata([ToolTag.READ_ONLY, output_tag]),
                )
            ]
        )
    )


async def _dispatch_store(
    db_engine: AsyncEngine, output_tag: ToolTag
) -> SourceTrustTier:
    db = Database(db_engine)
    registry = _registry(db_engine)
    context = tool_context(db, tracker_at(None), attachment_registry=registry)

    result = await _provider(output_tag).execute_tool(
        "store_output", {}, context, "call-1"
    )

    attachment_id = result.get_text() if isinstance(result, ToolResult) else result
    stored = await registry.get_attachment(db, attachment_id, acting_user_id=None)
    assert stored is not None
    return _stamped_tier(stored.metadata)


@pytest.mark.asyncio
async def test_a_tool_stored_attachment_carries_its_calls_declared_output_tier(
    db_engine: AsyncEngine,
) -> None:
    assert (
        await _dispatch_store(db_engine, ToolTag.OUTPUT_UNTRUSTED)
        is SourceTrustTier.UNKNOWN_EXTERNAL
    )


@pytest.mark.asyncio
async def test_a_tool_stored_attachment_from_a_clean_turn_is_internal(
    db_engine: AsyncEngine,
) -> None:
    assert (
        await _dispatch_store(db_engine, ToolTag.OUTPUT_TRUSTED)
        is SourceTrustTier.TRUSTED_INTERNAL
    )


@pytest.mark.asyncio
async def test_a_direct_user_upload_is_stamped_trusted_user(
    db_engine: AsyncEngine,
) -> None:
    registry = _registry(db_engine)

    stored = await registry.register_user_attachment(
        Database(db_engine),
        content=b"my photo",
        filename="photo.txt",
        mime_type="text/plain",
    )

    assert _stamped_tier(stored.metadata) is SourceTrustTier.TRUSTED_USER


@pytest.mark.asyncio
async def test_reading_a_reviewed_note_with_an_email_attachment_raises_the_turn(
    db_engine: AsyncEngine,
) -> None:
    """Review vouches for the note, not for the attachment's contents."""
    db = Database(db_engine)
    registry = _registry(db_engine)
    email_state = state_at(SourceTrustTier.KNOWN_CONTACT)
    attachment = await registry.store_and_register_tool_attachment(
        file_content=b"invoice",
        filename="invoice.txt",
        content_type="text/plain",
        tool_name="email",
        db_context=db,
        taint_state=email_state,
    )
    await db.notes.add_or_update(
        "Bills",
        "Pay the invoice.",
        True,
        attachment_ids=[attachment.attachment_id],
        # ast-grep-ignore: no-unconstrained-note-write-policy - test seeding, no profile in play
        write_policy=NoteWritePolicy.UNCONSTRAINED,
        provenance=NoteProvenanceStamp.admitted(title="Bills", decided_by="test"),
    )
    tracker = tracker_at(None)

    await get_note_tool(
        "Bills", tool_context(db, tracker, attachment_registry=registry)
    )

    assert tracker.snapshot().max_tier is SourceTrustTier.KNOWN_CONTACT
